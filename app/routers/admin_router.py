from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session, joinedload

from app.core.security import CurrentUser, require_admin
from app.core.time import iso_utc
from app.db.database import AuditLog, RFIProject, RFPProject, User, get_db
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
                "rfp_project_id": log.rfp_project_id,
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


def _trash_rfi_payload(doc: RFIProject) -> dict:
    return {
        "id": doc.id,
        "documentId": doc.slug or str(doc.id),
        "slug": doc.slug,
        "fileName": doc.filename,
        "filename": doc.filename,
        "status": doc.status,
        "type": "rfi",
        "is_deleted": bool(doc.is_deleted),
        "created_at": iso_utc(doc.created_at),
        "updated_at": iso_utc(doc.updated_at or doc.created_at),
        "deleted_at": iso_utc(doc.deleted_at),
        "owner": _user_payload(doc.user),
        "deleted_by": _user_payload(doc.deleted_by),
    }


def _trash_rfp_payload(project: RFPProject) -> dict:
    return {
        "id": project.id,
        "documentId": project.slug or str(project.id),
        "slug": project.slug,
        "product": project.product,
        "project_name": project.project_name,
        "status": project.status,
        "type": "rfp",
        "is_deleted": bool(project.is_deleted),
        "created_at": iso_utc(project.created_at),
        "updated_at": iso_utc(project.updated_at or project.created_at),
        "deleted_at": iso_utc(project.deleted_at),
        "owner": _user_payload(project.user),
        "deleted_by": _user_payload(project.deleted_by),
    }


@router.get("/projects")
async def list_admin_projects(
    type: Optional[str] = None,
    status: Optional[str] = None,
    include_deleted: bool = True,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(require_admin),
):
    """All RFI/RFP for admin overview (active + deleted by default)."""
    rfi_items: list[dict] = []
    rfp_items: list[dict] = []

    if type in (None, "rfi"):
        rfi_q = db.query(RFIProject).options(joinedload(RFIProject.user), joinedload(RFIProject.deleted_by))
        if not include_deleted:
            rfi_q = rfi_q.filter(RFIProject.is_deleted.is_(False))
        if status:
            rfi_q = rfi_q.filter(RFIProject.status == status)
        rfi_items = [_trash_rfi_payload(d) for d in rfi_q.order_by(desc(RFIProject.updated_at), desc(RFIProject.created_at)).all()]

    if type in (None, "rfp"):
        rfp_q = db.query(RFPProject).options(joinedload(RFPProject.user), joinedload(RFPProject.deleted_by))
        if not include_deleted:
            rfp_q = rfp_q.filter(RFPProject.is_deleted.is_(False))
        if status:
            rfp_q = rfp_q.filter(RFPProject.status == status)
        rfp_items = [_trash_rfp_payload(p) for p in rfp_q.order_by(desc(RFPProject.updated_at), desc(RFPProject.created_at)).all()]

    return {"rfi": rfi_items, "rfp": rfp_items}


@router.get("/trash")
async def list_trash(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(require_admin),
):
    """Deleted RFI and RFP projects available to recover."""
    rfi_docs = (
        db.query(RFIProject)
        .options(joinedload(RFIProject.user), joinedload(RFIProject.deleted_by))
        .filter(RFIProject.is_deleted.is_(True))
        .order_by(desc(RFIProject.deleted_at), desc(RFIProject.updated_at))
        .all()
    )
    rfp_projects = (
        db.query(RFPProject)
        .options(joinedload(RFPProject.user), joinedload(RFPProject.deleted_by))
        .filter(RFPProject.is_deleted.is_(True))
        .order_by(desc(RFPProject.deleted_at), desc(RFPProject.updated_at))
        .all()
    )
    return {
        "rfi": [_trash_rfi_payload(d) for d in rfi_docs],
        "rfp": [_trash_rfp_payload(p) for p in rfp_projects],
    }


@router.post("/rfi/{rfi_id}/restore")
async def restore_rfi(
    rfi_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    doc = db.query(RFIProject).filter(RFIProject.id == rfi_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="RFI not found")
    if not doc.is_deleted:
        return _trash_rfi_payload(doc)

    doc.is_deleted = False
    doc.deleted_at = None
    doc.deleted_by_user_id = None
    doc.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(doc)

    log_audit(
        db,
        user_id=current_user.id,
        action="rfi.restore",
        resource_type="rfi_project",
        rfi_project_id=doc.id,
        details={"filename": doc.filename, "slug": doc.slug},
        ip_address=_caller_ip(request),
    )
    return _trash_rfi_payload(doc)


@router.post("/rfp/{rfp_id}/restore")
async def restore_rfp(
    rfp_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    project = db.query(RFPProject).filter(RFPProject.id == rfp_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="RFP not found")
    if not project.is_deleted:
        return _trash_rfp_payload(project)

    project.is_deleted = False
    project.deleted_at = None
    project.deleted_by_user_id = None
    project.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=current_user.id,
        action="rfp.restore",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={"product": project.product, "project_name": project.project_name, "slug": project.slug},
        ip_address=_caller_ip(request),
    )
    return _trash_rfp_payload(project)


@router.delete("/rfi/{rfi_id}")
async def hard_delete_rfi(
    rfi_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    doc = db.query(RFIProject).filter(RFIProject.id == rfi_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="RFI not found")
    if not doc.is_deleted:
        raise HTTPException(status_code=400, detail="Soft-delete this RFI before permanently removing it")

    snapshot = {"filename": doc.filename, "slug": doc.slug, "id": doc.id}
    db.delete(doc)
    db.commit()

    log_audit(
        db,
        user_id=current_user.id,
        action="rfi.hard_delete",
        resource_type="rfi_project",
        details=snapshot,
        ip_address=_caller_ip(request),
    )
    return {"status": "permanently_deleted", "id": rfi_id}


@router.delete("/rfp/{rfp_id}")
async def hard_delete_rfp(
    rfp_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    project = db.query(RFPProject).filter(RFPProject.id == rfp_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="RFP not found")
    if not project.is_deleted:
        raise HTTPException(status_code=400, detail="Soft-delete this RFP before permanently removing it")

    snapshot = {
        "product": project.product,
        "project_name": project.project_name,
        "slug": project.slug,
        "id": project.id,
    }
    db.delete(project)
    db.commit()

    log_audit(
        db,
        user_id=current_user.id,
        action="rfp.hard_delete",
        resource_type="rfp_project",
        details=snapshot,
        ip_address=_caller_ip(request),
    )
    return {"status": "permanently_deleted", "id": rfp_id}
