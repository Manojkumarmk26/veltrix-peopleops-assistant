"""Document parsers for PDF, DOCX, Markdown, and HTML files.

Every parser normalises its output to a light Markdown dialect so that the
chunker can treat all formats the same way:

* headings become ``#`` lines (``#`` = document title, ``##`` = section),
* tables become pipe tables (``| a | b |``),
* PDF pages are separated by ``[Page N]`` markers (used for ``p. N`` locators).

Scanned PDF pages with no text layer are OCR'd (see ``app/ingestion/ocr.py``).
"""

import logging
import re
from pathlib import Path
from statistics import median

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz
from bs4 import BeautifulSoup
from docx import Document as DocxDocument

from app.ingestion.ocr import ocr_pdf_page

logger = logging.getLogger(__name__)

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_document(file_path: Path) -> str:
    """Parse a document file and return its text content.

    Supports: .pdf, .docx, .md, .html
    Automatically applies OCR for scanned PDFs with no text.
    """
    suffix = file_path.suffix.lower()

    parsers = {
        ".pdf": parse_pdf,
        ".docx": parse_docx,
        ".md": parse_markdown,
        ".markdown": parse_markdown,
        ".html": parse_html,
        ".htm": parse_html,
        ".txt": parse_markdown,
    }

    parser = parsers.get(suffix)
    if parser is None:
        logger.warning("Unsupported file format: %s", suffix)
        return ""

    try:
        text = parser(file_path)
        if text:
            text = clean_text(text)
        logger.info(
            "Parsed %s: %d characters", file_path.name, len(text) if text else 0
        )
        return text or ""
    except Exception as e:
        logger.error("Failed to parse %s: %s", file_path.name, e, exc_info=True)
        return ""


# ── PDF ─────────────────────────────────────────────────────────────────────


def parse_pdf(file_path: Path) -> str:
    """Parse a PDF file. Falls back to OCR for scanned pages.

    Tables are detected with PyMuPDF's table finder and emitted as pipe
    tables; text inside table areas is not repeated. Headings are detected
    from font size/weight relative to the body text of the document.
    """
    doc = fitz.open(str(file_path))
    try:
        body_size = _pdf_body_font_size(doc)
        pages_text = []

        for page_num, page in enumerate(doc, 1):
            raw_text = page.get_text("text")

            # If page has very little text, try OCR (likely a scanned document)
            if len(raw_text.strip()) < 50:
                logger.info(
                    "Page %d of %s has minimal text, attempting OCR...",
                    page_num,
                    file_path.name,
                )
                ocr_text = ocr_pdf_page(page)
                if ocr_text and len(ocr_text.strip()) > len(raw_text.strip()):
                    pages_text.append(f"[Page {page_num}]\n{ocr_text.strip()}")
                    continue

            structured = _pdf_page_to_markdown(page, body_size)
            text = structured if structured.strip() else raw_text
            if text.strip():
                pages_text.append(f"[Page {page_num}]\n{text.strip()}")
    finally:
        doc.close()
    return "\n\n".join(_strip_running_lines(pages_text))


def _strip_running_lines(pages: list[str]) -> list[str]:
    """Remove short lines repeated on every page (running headers/footers)."""
    if len(pages) < 2:
        return pages
    page_lines = [set(p.split("\n")[1:]) for p in pages]
    common = set.intersection(*page_lines)
    running = {
        ln for ln in common
        if ln.strip() and len(ln) < 100 and not ln.lstrip().startswith(("|", "#"))
    }
    if not running:
        return pages
    return [
        "\n".join(ln for ln in p.split("\n") if ln not in running) for p in pages
    ]


def _pdf_body_font_size(doc) -> float:
    """Most common (median) span size across the document = body text size."""
    sizes = []
    for page in doc:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        sizes.append(round(span["size"], 1))
    return median(sizes) if sizes else 10.0


def _pdf_page_to_markdown(page, body_size: float) -> str:
    """Convert one PDF page to Markdown-ish text with headings and tables."""
    try:
        tables = page.find_tables().tables
    except Exception as e:  # pragma: no cover - depends on PDF internals
        logger.debug("Table detection failed on page %s: %s", page.number + 1, e)
        tables = []
    table_rects = [fitz.Rect(t.bbox) for t in tables]

    items: list[tuple[float, str]] = []  # (y position, text)
    for t, rect in zip(tables, table_rects):
        md = _rows_to_markdown(_normalise_table_rows(t.extract()))
        if md:
            items.append((rect.y0, md))

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        block_rect = fitz.Rect(block["bbox"])
        if any(_mostly_inside(block_rect, tr) for tr in table_rects):
            continue
        lines_out = []
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            size = max(s["size"] for s in spans)
            bold = all(s.get("flags", 0) & 16 for s in spans)
            if size >= body_size + 5:
                lines_out.append(f"# {text}")
            elif size >= body_size + 1.5 and (bold or size >= body_size + 2.5):
                lines_out.append(f"## {text}")
            else:
                lines_out.append(text)
        if lines_out:
            items.append((block_rect.y0, "\n".join(lines_out)))

    items.sort(key=lambda it: it[0])
    return "\n\n".join(text for _, text in items)


def _mostly_inside(inner, outer) -> bool:
    """True if most of ``inner`` lies within ``outer``."""
    if inner.is_empty or inner.get_area() == 0:
        return False
    overlap = fitz.Rect(inner) & outer
    return overlap.get_area() / inner.get_area() > 0.6


def _normalise_table_rows(rows: list[list]) -> list[list[str]]:
    """Clean table cells; expand key/value tables stored as one multi-line row."""
    cleaned = [
        [re.sub(r"\s*\n\s*", " ", (c or "")).strip() for c in row] for row in rows if row
    ]
    # A single row whose cells hold N newline-separated values is really an
    # N-row key/value table (common in document header blocks).
    if len(rows) == 1 and rows[0]:
        split = [(c or "").split("\n") for c in rows[0]]
        lengths = {len(s) for s in split}
        if len(lengths) == 1 and lengths.pop() > 1:
            return [[s[i].strip() for s in split] for i in range(len(split[0]))]
    return [r for r in cleaned if any(r)]


# ── DOCX ────────────────────────────────────────────────────────────────────


def parse_docx(file_path: Path) -> str:
    """Parse a DOCX file including tables, preserving heading structure."""
    doc = DocxDocument(str(file_path))
    style_names = {s.style_id: (s.name or "") for s in doc.styles}
    parts = []

    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag

        if tag == "p":
            text = "".join(
                t.text for t in element.iter(f"{_W_NS}t") if t.text
            ).strip()
            if not text:
                continue
            style_id = None
            p_style = element.find(f"{_W_NS}pPr/{_W_NS}pStyle")
            if p_style is not None:
                style_id = p_style.get(f"{_W_NS}val")
            style = (style_names.get(style_id, style_id) or "").lower().replace(" ", "")
            if style == "title":
                parts.append(f"# {text}")
            elif style.startswith("heading"):
                level = re.sub(r"\D", "", style) or "1"
                parts.append(f"{'#' * min(int(level) + 1, 6)} {text}")
            elif "list" in style:
                parts.append(f"- {text}")
            else:
                parts.append(text)

        elif tag == "tbl":
            # Table — convert to markdown-style table
            table_text = _extract_docx_table(element)
            if table_text:
                parts.append(table_text)

    return "\n\n".join(parts)


def _extract_docx_table(tbl_element) -> str:
    """Extract a table from a DOCX element as a text representation."""
    rows = []
    for tr in tbl_element.findall(f".//{_W_NS}tr"):
        cells = []
        for tc in tr.findall(f".//{_W_NS}tc"):
            cell_texts = []
            for p in tc.findall(f".//{_W_NS}p"):
                cell_texts.append("".join(t.text for t in p.iter(f"{_W_NS}t") if t.text))
            cells.append(" ".join(cell_texts).strip())
        rows.append(cells)
    return _rows_to_markdown(rows)


# ── Markdown / HTML ─────────────────────────────────────────────────────────


def parse_markdown(file_path: Path) -> str:
    """Parse a Markdown file (read as-is, it's already text)."""
    return file_path.read_text(encoding="utf-8", errors="replace")


def parse_html(file_path: Path) -> str:
    """Parse an HTML file, extracting text and preserving headings and tables."""
    html_content = file_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html_content, "lxml")

    # Remove non-content elements
    for element in soup(["script", "style", "meta", "link", "noscript", "head"]):
        element.decompose()

    # Extract tables specially
    for table in soup.find_all("table"):
        table_text = _html_table_to_text(table)
        table.replace_with(soup.new_string("\n\n" + table_text + "\n\n"))

    # Headings → Markdown headings
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            text = h.get_text(" ", strip=True)
            h.replace_with(soup.new_string(f"\n\n{'#' * level} {text}\n\n"))

    for li in soup.find_all("li"):
        li.insert_before(soup.new_string("\n- "))
    for block in soup.find_all(["p", "div", "section", "article", "br"]):
        block.insert_after(soup.new_string("\n"))

    return soup.get_text()


def _html_table_to_text(table) -> str:
    """Convert an HTML table to a markdown-style text representation."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    return _rows_to_markdown(rows)


# ── Shared helpers ──────────────────────────────────────────────────────────


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """Render table rows as a pipe table (first row is the header)."""
    rows = [[(c or "").replace("|", "/").strip() for c in r] for r in rows if r]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(lines)


def clean_text(text: str) -> str:
    """Clean extracted text — normalize whitespace and remove artifacts."""
    # Replace multiple spaces with single space (but preserve newlines)
    text = re.sub(r"[^\S\n]+", " ", text)
    # Strip leading/trailing whitespace from each line
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    # Replace multiple newlines with double newline
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove leading/trailing whitespace
    return text.strip()
