"""Tests for the Markdown -> HTML -> PDF rendering pipeline."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.rfi.pdf_render import (  # noqa: E402  (sys.path edit)
    markdown_to_html,
    render_pdf_bytes,
)


class MarkdownToHtmlTests(unittest.TestCase):
    def test_headings_and_paragraphs(self) -> None:
        html = markdown_to_html("# Title\n\nHello world.")
        self.assertIn("<h1", html)
        self.assertIn("Title", html)
        self.assertIn("Hello world.", html)

    def test_tables_render(self) -> None:
        markdown = """\n| A | B |\n|---|---|\n| 1 | 2 |\n"""
        html = markdown_to_html(markdown)
        self.assertIn("<table>", html)
        self.assertIn("<th>", html)
        self.assertIn("<td>", html)

    def test_placeholders_become_badges(self) -> None:
        html = markdown_to_html("Body <!-- INSERT: project_name -->.")
        self.assertIn('class="placeholder"', html)
        self.assertIn("project_name", html)

    def test_empty_input(self) -> None:
        self.assertEqual(markdown_to_html(""), "")


class PdfRenderTests(unittest.TestCase):
    def test_render_produces_pdf(self) -> None:
        pdf = render_pdf_bytes("# Title\n\nPara.", title="Smoke")
        self.assertTrue(pdf.startswith(b"%PDF"), "Expected PDF magic bytes")
        self.assertGreater(len(pdf), 500)

    def test_render_includes_footer(self) -> None:
        pdf = render_pdf_bytes(
            "# Footer Test",
            title="Footer Test",
            footer="confidential",
        )
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
