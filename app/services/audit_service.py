"""Append-only audit log for authenticated actions."""

from __future__ import annotations

import logging
from typing import Any, Mapping, MutableMapping, Optional

from sqlalchemy.orm import Session

from app.db.database import AuditLog

logger = logging.getLogger(__name__)


def log_audit(
    db: Session,
    *,
    user_id: Optional[int],
    action: str,
    resource_type: str,
    document_id: Optional[int] = None,
    details: Optional[MutableMapping[str, Any]] = None,
    ip_address: Optional[str] = None,
    commit: bool = True,
) -> None:
    try:
        row = AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            document_id=document_id,
            details=dict(details) if details is not None else None,
            ip_address=ip_address,
        )
        db.add(row)
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception as e:
        if commit:
            db.rollback()
        logger.warning("Failed to write audit log (action=%s): %s", action, e)
