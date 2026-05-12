"""
Optional in-process Docling (Python library) per https://docling-project.github.io/docling/

Install separately: ``pip install -r requirements-docling-embedded.txt``
(heavy: PyTorch / models). Used when ``DOCLING_MODE=embedded`` or
``embedded_then_remote``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def convert_pdf_path_sync(
    path: str | os.PathLike[str],
    *,
    ocr_lang: list[str],
    force_full_page_ocr: bool,
    do_table_structure: bool = True,
) -> str:
    """Synchronously convert a PDF on disk to Markdown. Raises on failure."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    langs = [x.strip() for x in ocr_lang if x.strip()] or ["en"]
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = do_table_structure
    pipeline_options.do_ocr = True
    pipeline_options.ocr_options = EasyOcrOptions(lang=langs)
    pipeline_options.ocr_options.force_full_page_ocr = force_full_page_ocr

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )
    result = converter.convert(str(path))
    return result.document.export_to_markdown()


def try_convert_pdf_bytes(
    file_bytes: bytes,
    filename: str,
    *,
    ocr_lang: list[str],
    force_full_page_ocr: bool,
) -> str | None:
    """
    Write bytes to a temp ``.pdf``, run Docling, delete temp file.

    Returns ``None`` if the ``docling`` package is not installed.
    Raises if conversion fails after a successful import.
    """
    try:
        import tempfile
        import docling  # noqa: F401
    except ImportError:
        logger.info("Docling Python package not installed; skipping embedded conversion")
        return None

    suffix = ".pdf"
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="rfi-docling-")
        os.close(fd)
        Path(tmp).write_bytes(file_bytes)
        return convert_pdf_path_sync(
            tmp,
            ocr_lang=ocr_lang,
            force_full_page_ocr=force_full_page_ocr,
            do_table_structure=True,
        ).strip()
    finally:
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Temp PDF cleanup failed: %s", exc)
