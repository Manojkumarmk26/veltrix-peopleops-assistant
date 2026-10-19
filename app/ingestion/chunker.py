"""Text chunking module for document processing.

Works on the Markdown dialect produced by ``app/ingestion/parsers.py``:
``#`` headings, pipe tables and ``[Page N]`` markers.

Strategy (see DESIGN.md):
1. Split the document into sections at headings. Each section keeps its
   nearest heading as the citation locator; documents without headings use
   the page number (``p. N``).
2. A section that fits in ``max_tokens`` becomes one chunk, so a rule and its
   value are never separated. Longer sections are split on paragraph/table-row
   boundaries; table header rows are repeated in every piece.
3. Each chunk starts with a one-line context header (document title and
   section) so both the embedding and the LLM know where the text came from.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import tiktoken

logger = logging.getLogger(__name__)

# Use cl100k_base encoding as required by the task spec
_encoder = tiktoken.get_encoding("cl100k_base")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PAGE_RE = re.compile(r"^\[Page\s+(\d+)\]$")
_RUNNING_PAGE_RE = re.compile(r"^Page\s+\d+$", re.IGNORECASE)


@dataclass
class Chunk:
    """A chunk of document text with metadata."""

    doc_id: str
    chunk_id: int
    text: str
    token_count: int
    locator: str  # Section heading or page number for citations
    metadata: dict = field(default_factory=dict)


def count_tokens(text: str) -> int:
    """Count tokens using cl100k_base encoding."""
    return len(_encoder.encode(text))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to at most ``max_tokens`` cl100k_base tokens."""
    tokens = _encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return _encoder.decode(tokens[:max_tokens])


@dataclass
class _Section:
    heading: Optional[str]
    level: int
    page: int
    lines: list = field(default_factory=list)


def chunk_document(
    text: str,
    doc_id: str,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    metadata: Optional[dict] = None,
) -> list[Chunk]:
    """Split document text into section-aligned chunks.

    Args:
        text: Full document text (parser Markdown dialect)
        doc_id: Document identifier
        max_tokens: Maximum tokens per chunk body
        overlap_tokens: Overlap between consecutive pieces of a long section
        metadata: Additional metadata to attach to each chunk

    Returns:
        List of Chunk objects
    """
    if not text or not text.strip():
        return []

    metadata = metadata or {}
    title = metadata.get("title") or doc_id
    sections = _split_into_sections(text)
    has_headings = any(s.heading for s in sections)

    chunks: list[Chunk] = []
    for section in sections:
        body = "\n".join(section.lines).strip()
        if not body:
            continue
        if section.heading is None and has_headings and count_tokens(body) < 20:
            continue  # breadcrumb / running header before the first heading
        if section.heading and has_headings:
            locator = section.heading
        else:
            locator = f"p. {section.page}"

        header = f"[{title} | {locator}]"
        for piece in _split_body(body, max_tokens, overlap_tokens):
            chunk_text = f"{header}\n{piece}"
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    chunk_id=len(chunks),
                    text=chunk_text,
                    token_count=count_tokens(chunk_text),
                    locator=locator,
                    metadata=metadata.copy(),
                )
            )

    logger.info("Document %s chunked into %d chunks", doc_id, len(chunks))
    return chunks


def _split_into_sections(text: str) -> list[_Section]:
    """Split text into sections at Markdown headings, tracking page numbers."""
    page = 1
    current = _Section(heading=None, level=0, page=page)
    sections = [current]

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        page_match = _PAGE_RE.match(line.strip())
        if page_match:
            page = int(page_match.group(1))
            if not current.lines and current.heading is None:
                current.page = page
            continue
        if _RUNNING_PAGE_RE.match(line.strip()):
            continue  # running "Page N" footer/header

        heading_match = _HEADING_RE.match(line.strip())
        if heading_match:
            current = _Section(
                heading=heading_match.group(2).strip(),
                level=len(heading_match.group(1)),
                page=page,
            )
            sections.append(current)
            continue
        current.lines.append(line)

    return sections


def _split_body(body: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a section body into pieces of at most ``max_tokens`` tokens."""
    if count_tokens(body) <= max_tokens:
        return [body]

    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    units: list[str] = []
    for block in blocks:
        if count_tokens(block) <= max_tokens:
            units.append(block)
        elif _is_table(block):
            units.extend(_split_table(block, max_tokens))
        else:
            units.extend(_split_words(block, max_tokens, overlap_tokens))

    pieces: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if current and count_tokens(candidate) > max_tokens:
            pieces.append(current)
            overlap = _tail(current, overlap_tokens) if not _is_table(current) else ""
            current = f"{overlap}\n\n{unit}" if overlap else unit
            if count_tokens(current) > max_tokens:
                current = unit
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _is_table(block: str) -> bool:
    lines = [ln for ln in block.split("\n") if ln.strip()]
    return bool(lines) and all(ln.strip().startswith("|") for ln in lines)


def _split_table(block: str, max_tokens: int) -> list[str]:
    """Split a long pipe table into pieces, repeating the header rows."""
    lines = [ln for ln in block.split("\n") if ln.strip()]
    header = lines[:2] if len(lines) > 1 and set(lines[1].replace("|", "").strip()) <= {"-", " "} else lines[:1]
    rows = lines[len(header):]
    pieces, current = [], list(header)
    for row in rows:
        if len(current) > len(header) and count_tokens("\n".join(current + [row])) > max_tokens:
            pieces.append("\n".join(current))
            current = list(header)
        current.append(row)
    if len(current) > len(header):
        pieces.append("\n".join(current))
    return pieces


def _split_words(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a long block of prose on word boundaries with token overlap."""
    tokens = _encoder.encode(text)
    step = max(1, max_tokens - overlap_tokens)
    pieces = []
    for start in range(0, len(tokens), step):
        pieces.append(_encoder.decode(tokens[start:start + max_tokens]).strip())
        if start + max_tokens >= len(tokens):
            break
    return [p for p in pieces if p]


def _tail(text: str, overlap_tokens: int) -> str:
    """Return roughly the last ``overlap_tokens`` tokens, on a word boundary."""
    if overlap_tokens <= 0:
        return ""
    tokens = _encoder.encode(text)
    if len(tokens) <= overlap_tokens:
        return ""
    tail = _encoder.decode(tokens[-overlap_tokens:])
    return tail.split(" ", 1)[-1].strip() if " " in tail else tail.strip()
