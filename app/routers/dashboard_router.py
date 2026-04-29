from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.core.time import iso_utc
from app.db.database import get_db, AuditLog, RFIProject
from app.core.security import get_current_user, CurrentUser
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])

@router.get("/stats")
async def get_dashboard_stats(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user)
):
    if user.is_service_account:
        return {"total_rfi": 0, "generated_rfi": 0, "active_documents": 0}

    total_rfi = db.query(RFIProject).filter(RFIProject.user_id == user.id, RFIProject.is_deleted.is_(False)).count()
    generated_rfi = db.query(RFIProject).filter(
        RFIProject.user_id == user.id,
        RFIProject.status == "completed",
        RFIProject.is_deleted.is_(False),
    ).count()
    active_documents = db.query(RFIProject).filter(
        RFIProject.user_id == user.id,
        RFIProject.is_deleted.is_(False),
    ).count()

    return {
        "total_rfi": total_rfi,
        "generated_rfi": generated_rfi,
        "active_documents": active_documents
    }

@router.get("/history")
async def get_dashboard_history(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    limit: int = 10
):
    if user.is_service_account:
        return []

    logs = db.query(AuditLog).filter(AuditLog.user_id == user.id).order_by(desc(AuditLog.created_at)).limit(limit).all()
    
    result = []
    for log in logs:
        result.append({
            "id": log.id,
            "action": log.action,
            "resource_type": log.resource_type,
            "details": log.details,
            "created_at": iso_utc(log.created_at)
        })
    return result

class LogEventRequest(BaseModel):
    action: str
    resource_type: str
    details: Optional[dict] = None

from app.services.audit_service import log_audit

def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None

@router.post("/log-event")
async def log_custom_event(
    req: LogEventRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if user.is_service_account:
        return {"status": "ignored"}
    
    log_audit(
        db,
        user_id=user.id,
        action=req.action,
        resource_type=req.resource_type,
        details=req.details or {},
        ip_address=_caller_ip(request)
    )
    return {"status": "ok"}
