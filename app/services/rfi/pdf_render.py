"""Markdown → HTML → PDF rendering for the PDF-based RFI flow.

We use the `markdown` package for HTML conversion (with table support) and
`xhtml2pdf` for PDF generation. The CSS template targets clean A4 output.
"""

from __future__ import annotations

import io
import logging
import re
from html import escape

import markdown as md_lib
from xhtml2pdf import pisa

logger = logging.getLogger(__name__)

_PDF_CSS = """
@page {
    size: A4;
    margin: 22mm 20mm 25mm 20mm;
    @frame footer_frame {
        -pdf-frame-content: footer_content;
        left: 20mm;
        right: 20mm;
        top: 285mm;
        height: 8mm;
    }
}
body {
    font-family: "Helvetica", "Arial", sans-serif;
    color: #111827;
    font-size: 11pt;
    line-height: 1.55;
}
h1 {
    font-size: 22pt;
    color: #0f172a;
    border-bottom: 1.2pt solid #94a3b8;
    padding-bottom: 6pt;
    margin-top: 0;
    margin-bottom: 14pt;
    -pdf-keep-with-next: true;
}
h2 {
    font-size: 16pt;
    color: #1e293b;
    margin-top: 20pt;
    margin-bottom: 10pt;
    border-bottom: 0.6pt solid #cbd5e1;
    padding-bottom: 4pt;
    -pdf-keep-with-next: true;
}
h3 {
    font-size: 13pt;
    color: #1f2937;
    margin-top: 16pt;
    margin-bottom: 6pt;
    -pdf-keep-with-next: true;
}
h4, h5, h6 {
    color: #1f2937;
    margin-top: 12pt;
    margin-bottom: 4pt;
    -pdf-keep-with-next: true;
}
p {
    margin: 0 0 8pt 0;
    word-wrap: break-word;
    overflow-wrap: break-word;
}
ul, ol {
    margin: 4pt 0 10pt 18pt;
}
li {
    margin-bottom: 4pt;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 10pt 0 16pt 0;
    font-size: 10pt;
    -pdf-keep-in-frame-mode: shrink;
}
th, td {
    border: 0.6pt solid #94a3b8;
    padding: 6pt 8pt;
    vertical-align: top;
    text-align: left;
    word-wrap: break-word;
    overflow-wrap: break-word;
}
th {
    background-color: #e2e8f0;
    color: #0f172a;
    font-weight: 600;
    font-size: 10pt;
}
tr {
    page-break-inside: avoid;
}
.even-row td {
    background-color: #f8fafc;
}
blockquote {
    border-left: 2.4pt solid #94a3b8;
    padding-left: 10pt;
    color: #475569;
    margin: 8pt 0 12pt 0;
    font-style: italic;
}
code {
    font-family: "Courier", monospace;
    background-color: #f1f5f9;
    padding: 1pt 3pt;
    font-size: 10pt;
}
pre {
    background-color: #f1f5f9;
    padding: 10pt 12pt;
    font-size: 9pt;
    border-radius: 3pt;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 6pt 0 12pt 0;
}
.entity-chip {
    display: inline-block;
    background-color: #e0f2fe;
    border: 0.4pt solid #38bdf8;
    color: #0c4a6e;
    padding: 1pt 5pt;
    font-size: 9pt;
    margin: 0 1pt;
}
.placeholder {
    background-color: #fff7ed;
    border: 0.4pt dashed #fb923c;
    color: #9a3412;
    padding: 1pt 4pt;
    font-size: 9pt;
}
.footer {
    font-size: 8pt;
    color: #64748b;
    text-align: center;
}
hr {
    border: none;
    border-top: 0.6pt solid #cbd5e1;
    margin: 14pt 0;
}
"""


def _wrap_placeholders(html: str) -> str:
    """Render `<!-- INSERT: foo -->` placeholders as visible badges in the PDF."""

    def repl(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        return f'<span class="placeholder">[{escape(label)}]</span>'

    return re.sub(r"<!--\s*INSERT:\s*(.*?)\s*-->", repl, html, flags=re.DOTALL)


def _stripe_table_rows(html: str) -> str:
    """Add alternating row backgrounds via class attributes (xhtml2pdf has no nth-child)."""
    # Match each <table>…</table> block
    def stripe_table(table_match: re.Match[str]) -> str:
        table_html = table_match.group(0)
        row_idx = 0

        def tag_row(row_match: re.Match[str]) -> str:
            nonlocal row_idx
            tag = row_match.group(0)
            # Skip header rows (rows containing <th>)
            if "<th" in tag:
                return tag
            row_idx += 1
            if row_idx % 2 == 0:
                if 'class="' in tag:
                    return tag.replace('class="', 'class="even-row ')
                return tag.replace("<tr", '<tr class="even-row"', 1)
            return tag

        return re.sub(r"<tr[^>]*>.*?</tr>", tag_row, table_html, flags=re.DOTALL)

    return re.sub(r"<table[^>]*>.*?</table>", stripe_table, html, flags=re.DOTALL)


def markdown_to_html(markdown_text: str) -> str:
    """Convert markdown to HTML using extensions suitable for proposal documents."""
    if not markdown_text:
        return ""
    converter = md_lib.Markdown(
        extensions=["extra", "tables", "sane_lists", "fenced_code", "toc"],
        output_format="html5",
    )
    html = converter.convert(markdown_text)
    html = _wrap_placeholders(html)
    html = _stripe_table_rows(html)
    return html


def render_html_document(title: str, body_html: str, *, footer: str | None = None) -> str:
    """Wrap rendered body HTML into a complete printable HTML document."""
    safe_title = escape(title or "RFI Response")
    footer_html = (
        f'<div id="footer_content" class="footer">{escape(footer)}</div>'
        if footer
        else '<div id="footer_content" class="footer"></div>'
    )
    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset=\"utf-8\" />"
        f"<title>{safe_title}</title>"
        f"<style>{_PDF_CSS}</style>"
        "</head><body>"
        f"{body_html}"
        f"{footer_html}"
        "</body></html>"
    )


def render_pdf_bytes(markdown_text: str, *, title: str, footer: str | None = None) -> bytes:
    """Convert markdown to a finished PDF document in memory."""
    body_html = markdown_to_html(markdown_text)
    document_html = render_html_document(title, body_html, footer=footer)
    buffer = io.BytesIO()
    result = pisa.CreatePDF(src=document_html, dest=buffer, encoding="utf-8")
    if result.err:
        logger.error("xhtml2pdf reported %d errors while rendering RFI PDF", result.err)
        raise RuntimeError("PDF rendering failed")
    return buffer.getvalue()

