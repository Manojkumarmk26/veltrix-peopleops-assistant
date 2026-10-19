"""Main ingestion pipeline — orchestrates parsing, chunking, embedding, and indexing.

Ingestion is fully automatic: it reads ``MANIFEST_PATH`` and every file in
``CORPUS_DIR``. A fingerprint of the manifest, the corpus files and the
pipeline version decides whether a saved index can be reused; any change to
the corpus (or a fresh database without extracted holidays) triggers a
rebuild, so the service always reflects the data it was started with.
"""

import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.data_access.database import get_database
from app.data_access.leave_repo import HolidayRecord, LeaveRepository
from app.ingestion.chunker import Chunk, _split_into_sections, chunk_document
from app.ingestion.parsers import parse_document
from app.retrieval.embeddings import embed_texts
from app.retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Bump when parsing/chunking/embedding logic changes so stale indexes rebuild.
PIPELINE_VERSION = "3"
FINGERPRINT_FILE = "fingerprint.txt"


@dataclass
class DocumentMetadata:
    """Metadata for a document from manifest.json."""

    doc_id: str
    file: str
    title: str
    doc_type: str
    regions: list[str]
    effective_date: str
    access_level: str


VALID_DOC_TYPES = {"policy", "addendum", "memo", "handbook", "calendar", "faq", "wiki"}
VALID_ACCESS_LEVELS = {"all", "managers", "hr"}


# ── Public entry points ─────────────────────────────────────────────────────


def ensure_index() -> VectorStore:
    """Return a ready vector store, rebuilding whatever is stale.

    * Index fingerprint matches and the DB already has the holidays for this
      corpus → load from disk.
    * Index matches but the DB is fresh (e.g. replaced ./data/veltrix.db) →
      load the index and re-extract holidays only.
    * Otherwise → full ingestion.
    """
    settings = get_settings()
    manifest = load_manifest(settings.manifest_file)
    fingerprint = compute_fingerprint(manifest)
    store_dir = settings.vector_store_dir
    fp_file = store_dir / FINGERPRINT_FILE

    if fp_file.exists() and fp_file.read_text(encoding="utf-8").strip() == fingerprint:
        try:
            store = VectorStore.load(store_dir)
            db = get_database()
            if db.get_state("corpus_fingerprint") != fingerprint:
                logger.info("Database has no holidays for this corpus; re-extracting")
                _store_holidays(_extract_all_holidays(manifest, settings.corpus_path))
                _mark_complete(fingerprint)
            logger.info("Reusing vector store (fingerprint %s)", fingerprint[:12])
            return store
        except Exception as e:  # corrupted/partial index → rebuild
            logger.warning("Saved index unusable (%s); rebuilding", e)

    return run_ingestion(manifest=manifest, fingerprint=fingerprint)


def run_ingestion(
    manifest: Optional[list[dict]] = None,
    fingerprint: Optional[str] = None,
) -> VectorStore:
    """Run the full ingestion pipeline.

    Steps:
    1. Load manifest.json
    2. Parse each document
    3. Extract holiday calendars into the database
    4. Chunk all documents
    5. Generate embeddings
    6. Build FAISS index

    Returns:
        The populated VectorStore
    """
    started = time.monotonic()
    settings = get_settings()
    if manifest is None:
        manifest = load_manifest(settings.manifest_file)
    if fingerprint is None:
        fingerprint = compute_fingerprint(manifest)

    all_chunks: list[Chunk] = []
    all_doc_metadata: dict[str, dict] = {}
    holidays: list[HolidayRecord] = []

    logger.info("Starting ingestion of %d documents...", len(manifest))

    for doc_meta in manifest:
        file_path = settings.corpus_path / doc_meta["file"]

        if not file_path.exists():
            logger.error("File not found for %s: %s", doc_meta["doc_id"], file_path)
            continue

        logger.info("Processing: %s (%s)", doc_meta["doc_id"], doc_meta["file"])

        # Parse document (a single unreadable file must not stop ingestion)
        try:
            text = parse_document(file_path)
        except Exception as e:
            logger.error("Failed to parse %s: %s", doc_meta["doc_id"], e, exc_info=True)
            continue
        if not text:
            logger.warning("No text extracted from %s", doc_meta["file"])
            continue

        # Extract holidays from calendar documents
        if doc_meta["doc_type"] == "calendar":
            holidays.extend(extract_holidays(text, doc_meta))

        # Chunk document with metadata
        doc_metadata = {
            "doc_id": doc_meta["doc_id"],
            "title": doc_meta["title"],
            "doc_type": doc_meta["doc_type"],
            "regions": doc_meta["regions"],
            "effective_date": doc_meta["effective_date"],
            "access_level": doc_meta["access_level"],
        }

        chunks = chunk_document(
            text=text,
            doc_id=doc_meta["doc_id"],
            max_tokens=400,
            overlap_tokens=60,
            metadata=doc_metadata,
        )

        all_chunks.extend(chunks)
        all_doc_metadata[doc_meta["doc_id"]] = doc_metadata

    if not all_chunks:
        logger.error("No chunks generated from any document!")
        raise RuntimeError("Ingestion failed — no chunks created")

    logger.info("Total chunks: %d", len(all_chunks))

    # Generate embeddings for all chunks
    chunk_texts = [c.text for c in all_chunks]
    logger.info("Generating embeddings for %d chunks...", len(chunk_texts))
    embeddings = embed_texts(chunk_texts)
    logger.info("Embeddings generated: shape %s", embeddings.shape)

    # Build vector store
    vector_store = VectorStore(dimension=embeddings.shape[1])
    vector_store.add_chunks(all_chunks, embeddings)
    vector_store.doc_metadata = all_doc_metadata

    # Save to disk atomically (write to a temp dir, then swap)
    store_dir = settings.vector_store_dir
    tmp_dir = store_dir.with_name(store_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    vector_store.save(tmp_dir)
    (tmp_dir / FINGERPRINT_FILE).write_text(fingerprint, encoding="utf-8")
    if store_dir.exists():
        shutil.rmtree(store_dir)
    tmp_dir.rename(store_dir)

    # Holidays + completion marker in the database
    _store_holidays(holidays)
    _mark_complete(fingerprint)

    logger.info(
        "Ingestion complete in %.1fs: %d documents, %d chunks, %d holiday rows",
        time.monotonic() - started,
        len(all_doc_metadata),
        len(all_chunks),
        len(holidays),
    )
    return vector_store


def load_manifest(manifest_path: Path) -> list[dict]:
    """Load and validate manifest.json."""
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("manifest.json must be a JSON array")

    required_fields = {
        "doc_id",
        "file",
        "title",
        "doc_type",
        "regions",
        "effective_date",
        "access_level",
    }
    seen_ids = set()
    valid = []
    for entry in data:
        missing = required_fields - set(entry.keys())
        if missing:
            raise ValueError(
                f"Document {entry.get('doc_id', '?')} missing fields: {missing}"
            )
        if entry["doc_id"] in seen_ids:
            raise ValueError(f"Duplicate doc_id in manifest: {entry['doc_id']}")
        seen_ids.add(entry["doc_id"])
        if entry["doc_type"] not in VALID_DOC_TYPES:
            logger.warning("Unknown doc_type %r for %s", entry["doc_type"], entry["doc_id"])
        if entry["access_level"] not in VALID_ACCESS_LEVELS:
            # Fail closed: an unknown access level is treated as HR-only.
            logger.warning(
                "Unknown access_level %r for %s; treating as 'hr'",
                entry["access_level"], entry["doc_id"],
            )
            entry = {**entry, "access_level": "hr"}
        if isinstance(entry["regions"], str):
            entry = {**entry, "regions": [entry["regions"]]}
        valid.append(entry)

    logger.info("Loaded manifest with %d documents", len(valid))
    return valid


def compute_fingerprint(manifest: list[dict]) -> str:
    """Hash of pipeline version, embedding model, manifest and corpus files."""
    settings = get_settings()
    h = hashlib.sha256()
    h.update(f"pipeline={PIPELINE_VERSION};model={settings.embedding_model}".encode())
    h.update(json.dumps(manifest, sort_keys=True).encode("utf-8"))
    for entry in sorted(manifest, key=lambda e: e["file"]):
        path = settings.corpus_path / entry["file"]
        h.update(entry["file"].encode("utf-8"))
        if path.exists():
            h.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            h.update(b"<missing>")
    return h.hexdigest()


# ── Holiday extraction ──────────────────────────────────────────────────────

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]
# Sections/tables of optional days that do not close the office.
_NON_CLOSURE = re.compile(r"\b(restricted|optional|floating)\b", re.IGNORECASE)
_NAME_COLUMNS = ("holiday", "name", "occasion", "festival", "event", "description")


def _parse_date_str(s: str, default_year: int) -> str:
    """Parse date string into ISO YYYY-MM-DD format."""
    s = s.strip()
    if not s:
        return ""

    # ISO format: 2026-01-26
    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m_iso:
        return s

    # "15 January 2026" or "15 January"
    m_dm = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?(?:,?\s+(\d{4}))?", s)
    if m_dm:
        day = int(m_dm.group(1))
        month_str = m_dm.group(2).lower()
        yr = int(m_dm.group(3)) if m_dm.group(3) else default_year
        for idx, m in enumerate(_MONTH_NAMES, 1):
            if month_str.startswith(m[:3]):
                return _valid_iso(yr, idx, day)

    # "January 15, 2026" or "January 15"
    m_md = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", s)
    if m_md:
        month_str = m_md.group(1).lower()
        day = int(m_md.group(2))
        yr = int(m_md.group(3)) if m_md.group(3) else default_year
        for idx, m in enumerate(_MONTH_NAMES, 1):
            if month_str.startswith(m[:3]):
                return _valid_iso(yr, idx, day)

    return ""


def _valid_iso(year: int, month: int, day: int) -> str:
    from datetime import date as _date

    try:
        return _date(year, month, day).isoformat()
    except ValueError:
        return ""


def _iter_tables(lines: list[str]):
    """Yield pipe tables (list of rows of cells) found in a list of lines."""
    table: list[list[str]] = []
    for line in lines + [""]:
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue  # separator row
            table.append(cells)
        elif table:
            yield table
            table = []


def extract_holidays(text: str, doc_meta: dict) -> list[HolidayRecord]:
    """Extract office-closure holidays from a calendar document.

    Supports:
    1. Pipe tables (from PDF/DOCX/HTML/Markdown) with a "Date" column.
    2. Multi-line patterns from PDF text: <Date> \\n <Day-of-Week> \\n <Holiday-Name>
    Sections or tables about restricted/optional holidays are ignored because
    they do not close the office.
    """
    year_match = re.search(r"20\d{2}", doc_meta.get("title", ""))
    default_year = int(year_match.group()) if year_match else int(doc_meta["effective_date"][:4])

    found: dict[str, str] = {}
    for section in _split_into_sections(text):
        if section.heading and _NON_CLOSURE.search(section.heading):
            continue
        for table in _iter_tables(section.lines):
            header = [h.lower() for h in table[0]]
            if any(_NON_CLOSURE.search(h) for h in header):
                continue
            date_col = next((i for i, h in enumerate(header) if "date" in h), None)
            if date_col is None:
                continue
            name_col = next(
                (i for i, h in enumerate(header)
                 if i != date_col and any(k in h for k in _NAME_COLUMNS)),
                len(header) - 1,
            )
            for row in table[1:]:
                if date_col >= len(row):
                    continue
                iso = _parse_date_str(row[date_col], default_year)
                if iso and iso not in found:
                    name = row[name_col] if name_col < len(row) else ""
                    found[iso] = name or "Public holiday"

    if not found:
        # Fallback for plain-text layouts: <Date>\n<Weekday>\n<Name>
        cut = _NON_CLOSURE.split(text, maxsplit=1)[0]
        pattern_3line = re.compile(
            r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\s*\n\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*\n\s*([^\n]+)",
            re.IGNORECASE,
        )
        for match in pattern_3line.finditer(cut):
            iso = _parse_date_str(match.group(1), default_year)
            if iso and iso not in found:
                found[iso] = match.group(3).strip()

    records = [
        HolidayRecord(
            region=region,
            holiday_date=iso,
            holiday_name=name,
            year=int(iso[:4]),
            source_doc_id=doc_meta["doc_id"],
            doc_effective_date=doc_meta["effective_date"],
            doc_access_level=doc_meta["access_level"],
        )
        for iso, name in sorted(found.items())
        for region in doc_meta.get("regions", [])
    ]
    logger.info(
        "Extracted %d holidays from %s for regions %s",
        len(found), doc_meta["doc_id"], doc_meta.get("regions"),
    )
    if not found:
        logger.warning("Calendar %s produced no holidays", doc_meta["doc_id"])
    return records


def _extract_all_holidays(manifest: list[dict], corpus_dir: Path) -> list[HolidayRecord]:
    records: list[HolidayRecord] = []
    for doc_meta in manifest:
        if doc_meta["doc_type"] != "calendar":
            continue
        path = corpus_dir / doc_meta["file"]
        if not path.exists():
            logger.error("Calendar file missing: %s", path)
            continue
        try:
            text = parse_document(path)
        except Exception as e:
            logger.error("Failed to parse calendar %s: %s", doc_meta["doc_id"], e)
            continue
        if text:
            records.extend(extract_holidays(text, doc_meta))
    return records


def _store_holidays(records: list[HolidayRecord]) -> None:
    n = LeaveRepository().replace_holidays(records)
    logger.info("Stored %d holiday rows", n)


def _mark_complete(fingerprint: str) -> None:
    db = get_database()
    db.set_state("corpus_fingerprint", fingerprint)
    db.set_state("status", "complete")
