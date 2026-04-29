from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session, joinedload

from app.core.security import CurrentUser, require_admin
from app.core.time import iso_utc
from app.db.database import AuditLog, User, get_db
from app.services.audit_service import log_audit


router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


class AdminFlagRequest(BaseModel):
    is_admin: bool


def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_payload(user: User | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "verified": user.verified,
        "is_admin": user.is_admin,
        "created_at": iso_utc(user.created_at),
        "updated_at": iso_utc(user.updated_at),
    }


@router.get("/users")
async def list_users(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(require_admin),
):
    users = db.query(User).order_by(User.name.asc().nullslast(), User.email.asc()).all()
    return [_user_payload(user) for user in users]


@router.put("/users/{user_id}/admin")
async def set_user_admin(
    user_id: int,
    req: AdminFlagRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        return {"status": "not_found"}

    target.is_admin = req.is_admin
    db.commit()
    db.refresh(target)

    log_audit(
        db,
        user_id=current_user.id,
        action="admin.user_role_update",
        resource_type="user",
        details={
            "target_user_id": target.id,
            "target_email": target.email,
            "is_admin": target.is_admin,
        },
        ip_address=_caller_ip(request),
    )
    return _user_payload(target)


@router.get("/audit-logs")
async def list_audit_logs(
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(require_admin),
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    query = db.query(AuditLog).options(joinedload(AuditLog.user))
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if date_from is not None:
        query = query.filter(AuditLog.created_at >= date_from)
    if date_to is not None:
        query = query.filter(AuditLog.created_at <= date_to)
    if q:
        needle = f"%{q}%"
        query = query.filter(
            or_(
                AuditLog.action.ilike(needle),
                AuditLog.resource_type.ilike(needle),
                AuditLog.ip_address.ilike(needle),
            )
        )

    total = query.count()
    logs = (
        query.order_by(desc(AuditLog.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "items": [
            {
                "id": log.id,
                "user": _user_payload(log.user),
                "action": log.action,
                "resource_type": log.resource_type,
                "document_id": log.document_id,
                "rfi_project_id": log.rfi_project_id,
                "details": log.details or {},
                "ip_address": log.ip_address,
                "created_at": iso_utc(log.created_at),
            }
            for log in logs
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/audit-actions")
async def list_audit_actions(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(require_admin),
):
    actions = db.query(AuditLog.action).distinct().order_by(AuditLog.action.asc()).all()
    resources = (
        db.query(AuditLog.resource_type)
        .distinct()
        .order_by(AuditLog.resource_type.asc())
        .all()
    )
    return {
        "actions": [row[0] for row in actions],
        "resource_types": [row[0] for row in resources],
    }
