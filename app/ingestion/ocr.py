"""OCR module for scanned document processing."""

import io
import logging
from pathlib import Path
from typing import Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz
from PIL import Image

logger = logging.getLogger(__name__)

# Try to import pytesseract — it's optional if not processing scanned docs
try:
    import pytesseract

    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False
    logger.warning("pytesseract not available. OCR will be disabled.")


def ocr_pdf_page(page: fitz.Page, dpi: int = 300) -> Optional[str]:
    """Apply OCR to a PDF page.

    Renders the page as an image and uses Tesseract for OCR.

    Args:
        page: PyMuPDF page object
        dpi: Resolution for rendering (higher = better OCR, slower)

    Returns:
        Extracted text or None if OCR fails
    """
    if not TESSERACT_AVAILABLE:
        logger.warning("Cannot OCR page — pytesseract not installed")
        return None

    try:
        # Render page as a high-resolution image
        zoom = dpi / 72  # 72 is default DPI
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)

        # Convert to PIL Image
        img_data = pix.tobytes("png")
        image = Image.open(io.BytesIO(img_data))

        # Run Tesseract OCR
        text = pytesseract.image_to_string(image, lang="eng")

        if text and text.strip():
            logger.info("OCR extracted %d characters from page", len(text.strip()))
            return text.strip()

        return None

    except Exception as e:
        logger.error("OCR failed for page: %s", e)
        return None


def ocr_full_pdf(pdf_path: Path, dpi: int = 300) -> str:
    """Apply OCR to an entire PDF file.

    Args:
        pdf_path: Path to the PDF file
        dpi: Resolution for rendering

    Returns:
        Concatenated text from all pages
    """
    doc = fitz.open(str(pdf_path))
    pages_text = []

    for page_num, page in enumerate(doc, 1):
        text = ocr_pdf_page(page, dpi)
        if text:
            pages_text.append(f"[Page {page_num}]\n{text}")

    doc.close()
    return "\n\n".join(pages_text)
