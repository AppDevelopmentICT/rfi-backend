"""Best-effort plain text extraction from PDF bytes (native text layer only).

Used when Docling yields little usable Markdown—e.g. odd glyph streams that look
like text in a viewer but are not surfaced well through layout parsers.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def extract_pdf_native_text_plain(file_bytes: bytes) -> str:
    """Return `\n\n` joined page texts, or empty string if PyMuPDF is unavailable/unreadable."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.debug("PyMuPDF not installed; skipping native PDF text fallback")
        return ""

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        logger.warning("PyMuPDF could not open PDF for text fallback: %s", exc)
        return ""

    parts: list[str] = []
    try:
        for page in doc:
            try:
                block = page.get_text("text") or ""
            except Exception:
                block = ""
            block = block.strip()
            if block:
                parts.append(block)
    finally:
        doc.close()

    return "\n\n".join(parts).strip()
