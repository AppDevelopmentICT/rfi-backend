"""Multicast fan-out for live PDF-draft LLM chunks (RFI PDF WebSocket clients).

Each pipeline run emits JSON-serializable events per project id. Connecting clients
receive a copy via their own asyncio queue.
"""

from __future__ import annotations

import asyncio
from typing import Any

_lock = asyncio.Lock()
_listeners: dict[int, list[asyncio.Queue[dict[str, Any]]]] = {}
_cancelled: dict[int, bool] = {}


async def subscribe_pdf_draft_stream(project_id: int) -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with _lock:
        _listeners.setdefault(project_id, []).append(q)
    return q


async def unsubscribe_pdf_draft_stream(project_id: int, q: asyncio.Queue) -> None:
    async with _lock:
        lst = _listeners.get(project_id)
        if not lst:
            return
        try:
            lst.remove(q)
        except ValueError:
            return
        if not lst:
            del _listeners[project_id]


async def broadcast_pdf_draft(project_id: int, message: dict[str, Any]) -> None:
    async with _lock:
        queues = list(_listeners.get(project_id, []))
    for q in queues:
        await q.put(message)


async def cancel_pdf_draft(project_id: int) -> None:
    """Signal the running pipeline for *project_id* to stop streaming."""
    async with _lock:
        _cancelled[project_id] = True


def is_pdf_draft_cancelled(project_id: int) -> bool:
    """Check (non-async, lock-free read) whether cancellation was requested."""
    return _cancelled.get(project_id, False)


async def clear_pdf_draft_cancel(project_id: int) -> None:
    """Reset the cancellation flag — call at the start of a new pipeline run."""
    async with _lock:
        _cancelled.pop(project_id, None)

