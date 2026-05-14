"""Behavioural tests for the master-data helpers using a stubbed DB session."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.rfi.master_data import list_engineers, list_projects  # noqa: E402


def _make_session_without_tables() -> MagicMock:
    session = MagicMock()
    # `to_regclass(:name)` returns None when the table is missing.
    session.execute.return_value.first.return_value = (None,)
    return session


class MissingMasterTablesTests(unittest.TestCase):
    """When the master tables are not present we should degrade gracefully."""

    def test_list_projects_returns_empty(self) -> None:
        session = _make_session_without_tables()
        result = list_projects(session, search="anything", limit=5, offset=0)
        self.assertEqual(result, {"items": [], "total": 0, "limit": 5, "offset": 0})

    def test_list_engineers_returns_empty(self) -> None:
        session = _make_session_without_tables()
        result = list_engineers(session, search="anything", limit=5, offset=0)
        self.assertEqual(result, {"items": [], "total": 0, "limit": 5, "offset": 0})


if __name__ == "__main__":
    unittest.main()
