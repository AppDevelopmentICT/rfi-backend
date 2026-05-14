"""Unit tests for the LLM-driven extraction helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.rfi.pdf_extraction import (  # noqa: E402
    _safe_json_loads,
    _strip_code_fences,
    _truncate_for_prompt,
)


class HelpersTests(unittest.TestCase):
    def test_truncate_keeps_short_input(self) -> None:
        text = "Hello world"
        self.assertEqual(_truncate_for_prompt(text, max_chars=50), text)

    def test_truncate_long_input_keeps_head_and_tail(self) -> None:
        text = "A" * 5000 + "B" * 5000
        truncated = _truncate_for_prompt(text, max_chars=2000)
        self.assertLess(len(truncated), 4096)
        self.assertIn("truncated", truncated.lower())
        self.assertIn("A", truncated)
        self.assertIn("B", truncated)

    def test_strip_code_fences_removes_markdown_block(self) -> None:
        wrapped = "```json\n{\"a\": 1}\n```"
        self.assertEqual(_strip_code_fences(wrapped), "{\"a\": 1}")

    def test_safe_json_loads_handles_plain_json(self) -> None:
        result = _safe_json_loads('{"requirements": []}')
        self.assertEqual(result, {"requirements": []})

    def test_safe_json_loads_handles_noisy_text(self) -> None:
        result = _safe_json_loads(
            "Here is the json:\n```json\n{\"k\": 1}\n```\nThank you."
        )
        self.assertEqual(result, {"k": 1})

    def test_safe_json_loads_returns_none_on_garbage(self) -> None:
        self.assertIsNone(_safe_json_loads("not json at all"))


if __name__ == "__main__":
    unittest.main()
