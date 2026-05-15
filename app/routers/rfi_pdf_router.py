"""HTTP API for the PDF-based RFI flow.

Endpoints are mounted under `/api/v1/rfi-pdf` and exist alongside (not on top
of) the existing Excel RFI router. The pipeline is:

1. `POST /upload` — store the PDF, kick off Docling parsing + LLM extraction
   in the background, return the new project payload immediately.
2. `GET /list`, `GET /list/mine`, `GET /{key}` — list / fetch projects.
3. `POST /{key}/lock` / `DELETE /{key}/lock` — collaborative edit locks.
4. `POST /{key}/save` — save the editor markdown.
5. `POST /{key}/regenerate` — re-run the LLM draft against the parsed text.
6. `GET /{key}/preview` and `POST /{key}/export` — render the live preview
   and the final downloadable PDF.
7. `GET /{key}/timeline` — audit-log derived activity stream.
8. `GET /master-data/projects` and `GET /master-data/engineers` — sidebar
   filter feeds for drag/drop.
9. `WS /ws/draft-stream/{key}` — live streamed AI draft deltas while the backend
   calls Ollama (pass auth token via query ``?token=``).
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from app.config import API_AUTH_SECRET
from app.core.security import CurrentUser, exchange_pocketbase_token, get_current_user
from app.core.time import iso_utc
from app.db.database import AuditLog, RFIPdfProject, SessionLocal, User, get_db
from app.services.audit_service import log_audit
from app.services.external.docling import parse_document
from app.services.rfi.master_data import list_engineers, list_projects
from app.services.rfi.pdf_extraction import draft_response_markdown, extract_requirements, GenerationCancelled
from app.services.rfi.pdf_render import render_pdf_bytes
from app.services.rfi.pdf_draft_stream_bus import (
    broadcast_pdf_draft,
    cancel_pdf_draft,
    clear_pdf_draft_cancel,
    is_pdf_draft_cancelled,
    subscribe_pdf_draft_stream,
    unsubscribe_pdf_draft_stream,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rfi-pdf", tags=["RFI/PDF"])

LOCK_TIMEOUT = timedelta(minutes=30)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB hard cap; PDFs above that are unusual.

STATUS_UPLOADING = "uploading"
STATUS_PARSING = "parsing"
STATUS_EXTRACTING = "extracting"
STATUS_GENERATING = "generating"
STATUS_DRAFTING = "drafting"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _pdf_project_readable_ws(user: CurrentUser, project: RFIPdfProject) -> bool:
    """Same effective access rule as authenticated HTTP endpoints for editors."""
    if user.is_service_account or user.is_admin:
        return True
    return bool(user.id is not None and user.id == project.user_id)


async def _websocket_current_user(raw_token: str) -> CurrentUser | None:
    token = (raw_token or "").strip()
    if not token:
        return None
    if token == API_AUTH_SECRET:
        return CurrentUser(
            id=None,
            pocketbase_id=None,
            email=None,
            name=None,
            is_admin=True,
            is_service_account=True,
        )
    try:
        return await exchange_pocketbase_token(token)
    except HTTPException:
        return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "rfi-pdf"


def _unique_slug(db: Session, base: str, exclude_project_id: int | None = None) -> str:
    base_slug = _slugify(base)
    candidate = base_slug
    suffix = 1
    while True:
        query = db.query(RFIPdfProject).filter(RFIPdfProject.slug == candidate)
        if exclude_project_id is not None:
            query = query.filter(RFIPdfProject.id != exclude_project_id)
        if query.first() is None:
            return candidate
        candidate = f"{base_slug}-{suffix}"
        suffix += 1


def _ensure_slug(db: Session, project: RFIPdfProject) -> RFIPdfProject:
    if project.slug:
        return project
    project.slug = _unique_slug(
        db,
        project.filename or f"rfi-pdf-{project.id}",
        exclude_project_id=project.id,
    )
    db.commit()
    db.refresh(project)
    return project


def _get_project_by_key(
    db: Session,
    key: str,
    *,
    include_deleted: bool = False,
) -> RFIPdfProject | None:
    query = db.query(RFIPdfProject).options(
        joinedload(RFIPdfProject.user),
        joinedload(RFIPdfProject.editing_user),
    )
    if not include_deleted:
        query = query.filter(RFIPdfProject.is_deleted.is_(False))
    project: RFIPdfProject | None = None
    if key.isdigit():
        project = query.filter(RFIPdfProject.id == int(key)).first()
    if project is None:
        project = query.filter(RFIPdfProject.slug == key).first()
    if project is not None:
        project = _ensure_slug(db, project)
    return project


def _user_payload(user: User | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name or user.email,
        "avatar_url": user.avatar_url,
        "is_admin": user.is_admin,
    }


def _is_lock_expired(project: RFIPdfProject) -> bool:
    if not project.editing_user_id or not project.lock_acquired_at:
        return False
    lock_time = project.lock_acquired_at
    if lock_time.tzinfo is None:
        lock_time = lock_time.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - lock_time > LOCK_TIMEOUT


def _release_expired_lock(db: Session, project: RFIPdfProject) -> bool:
    if not _is_lock_expired(project):
        return False
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    db.refresh(project)
    return True


def _require_lock_owner(db: Session, project: RFIPdfProject, user: CurrentUser) -> None:
    _release_expired_lock(db, project)
    if project.editing_user_id != user.id:
        holder_name = (
            project.editing_user.name if project.editing_user else "Another user"
        )
        raise HTTPException(
            status_code=409,
            detail=f"{holder_name} is still editing this RFI",
        )


def _project_payload(project: RFIPdfProject, current_user: CurrentUser | None = None) -> dict:
    locked_by_other = bool(
        current_user
        and project.editing_user_id
        and project.editing_user_id != current_user.id
    )
    metadata = project.metadata_json or {}
    return {
        "documentId": project.slug or str(project.id),
        "id": project.id,
        "slug": project.slug,
        "fileName": project.filename,
        "filename": project.filename,
        "status": project.status,
        "error_message": project.error_message,
        "title": metadata.get("title") or project.filename or "Untitled RFI",
        "summary": metadata.get("summary"),
        "language": metadata.get("language") or "en",
        "parsed_markdown": project.parsed_markdown or "",
        "editor_markdown": project.editor_markdown or "",
        "editor_html": project.editor_html or "",
        "requirements": project.requirements or [],
        "entity_refs": project.entity_refs or [],
        "metadata": metadata,
        "created_at": iso_utc(project.created_at),
        "updated_at": iso_utc(project.updated_at or project.created_at),
        "user": _user_payload(project.user),
        "uploaded_by": _user_payload(project.user),
        "editing_user": _user_payload(project.editing_user),
        "lock_acquired_at": iso_utc(project.lock_acquired_at),
        "is_locked_by_other": locked_by_other,
        "is_lock_held_by_me": bool(
            current_user and project.editing_user_id == current_user.id
        ),
    }


def _update_status(
    db: Session,
    project_id: int,
    status: str,
    *,
    error: str | None = None,
    extra: dict | None = None,
) -> RFIPdfProject | None:
    project = db.query(RFIPdfProject).filter(RFIPdfProject.id == project_id).first()
    if not project:
        return None
    project.status = status
    project.error_message = error
    project.updated_at = datetime.now(timezone.utc)
    if extra:
        for field, value in extra.items():
            setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return project


async def _run_pdf_pipeline(
    project_id: int,
    file_bytes: bytes,
    original_filename: str,
    user_id: int | None,
    ip_address: str | None,
) -> None:
    """Async pipeline: Docling parse → requirement extraction → draft generation."""
    await clear_pdf_draft_cancel(project_id)
    db = SessionLocal()
    try:
        _update_status(db, project_id, STATUS_PARSING)
        try:
            parsed_markdown = await parse_document(file_bytes, original_filename)
        except Exception as exc:
            logger.error("Docling parse failed for %s: %s", original_filename, exc, exc_info=True)
            _update_status(
                db,
                project_id,
                STATUS_FAILED,
                error=f"Failed to parse PDF: {exc}",
            )
            log_audit(
                db,
                user_id=user_id,
                action="rfi_pdf.parse_failed",
                resource_type="rfi_pdf_project",
                rfi_pdf_project_id=project_id,
                details={"error": str(exc), "filename": original_filename},
                ip_address=ip_address,
            )
            return

        _update_status(
            db,
            project_id,
            STATUS_EXTRACTING,
            extra={"parsed_markdown": parsed_markdown},
        )

        extraction = await extract_requirements(parsed_markdown)
        _update_status(
            db,
            project_id,
            STATUS_DRAFTING,
            extra={
                "requirements": extraction.get("requirements", []),
                "metadata_json": {
                    "title": extraction.get("title"),
                    "summary": extraction.get("summary"),
                    "language": extraction.get("language"),
                    "warning": extraction.get("warning"),
                },
            },
        )

        await broadcast_pdf_draft(project_id, {"type": "phase", "phase": "draft_started"})

        async def emit_delta(delta: str) -> None:
            await broadcast_pdf_draft(project_id, {"type": "draft_delta", "delta": delta})

        try:
            draft_markdown = await draft_response_markdown(
                parsed_markdown, extraction, on_stream_delta=emit_delta,
                cancel_check=lambda: is_pdf_draft_cancelled(project_id),
            )
        except GenerationCancelled:
            logger.info("Pipeline cancelled for project %s", project_id)
            await broadcast_pdf_draft(project_id, {"type": "draft_complete"})
            return

        project = db.query(RFIPdfProject).filter(RFIPdfProject.id == project_id).first()
        if not project:
            return
        project.editor_markdown = draft_markdown
        project.editor_html = None
        project.status = STATUS_READY
        project.error_message = None
        project.updated_at = datetime.now(timezone.utc)
        flag_modified(project, "requirements")
        flag_modified(project, "metadata_json")
        db.commit()
        await broadcast_pdf_draft(project_id, {"type": "draft_complete"})

        log_audit(
            db,
            user_id=user_id,
            action="rfi_pdf.generated",
            resource_type="rfi_pdf_project",
            rfi_pdf_project_id=project_id,
            details={
                "filename": original_filename,
                "requirements_count": len(extraction.get("requirements", [])),
                "warning": extraction.get("warning"),
            },
            ip_address=ip_address,
        )
    except Exception as exc:
        logger.error("RFI PDF pipeline failed: %s", exc, exc_info=True)
        try:
            await broadcast_pdf_draft(project_id, {"type": "draft_error", "message": str(exc)})
        except Exception:
            pass
        _update_status(
            db,
            project_id,
            STATUS_FAILED,
            error=f"Pipeline failed: {exc}",
        )
        log_audit(
            db,
            user_id=user_id,
            action="rfi_pdf.pipeline_failed",
            resource_type="rfi_pdf_project",
            rfi_pdf_project_id=project_id,
            details={"error": str(exc), "filename": original_filename},
            ip_address=ip_address,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SaveDraftRequest(BaseModel):
    editor_markdown: str = Field(..., max_length=400_000)
    editor_html: Optional[str] = Field(default=None, max_length=600_000)
    entity_refs: Optional[list[dict]] = None
    metadata: Optional[dict] = None


class RegenerateRequest(BaseModel):
    model: Optional[str] = None
    extra_instructions: Optional[str] = None


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post("/upload")
async def upload_rfi_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF file containing the RFI source document"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only .pdf files are accepted")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than the allowed maximum ({MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )

    base_stem = filename.rsplit(".", 1)[0]
    slug = _unique_slug(db, base_stem)
    project = RFIPdfProject(
        filename=filename,
        slug=slug,
        status=STATUS_UPLOADING,
        user_id=user.id if not user.is_service_account else None,
        requirements=[],
        entity_refs=[],
        metadata_json={},
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.uploaded",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={"filename": filename, "size_bytes": len(file_bytes)},
        ip_address=_caller_ip(request),
    )

    background_tasks.add_task(
        _run_pdf_pipeline,
        project.id,
        file_bytes,
        filename,
        user.id if not user.is_service_account else None,
        _caller_ip(request),
    )

    return _project_payload(project, user)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@router.get("/list")
async def list_rfi_pdfs(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    projects = (
        db.query(RFIPdfProject)
        .options(
            joinedload(RFIPdfProject.user),
            joinedload(RFIPdfProject.editing_user),
        )
        .filter(RFIPdfProject.is_deleted.is_(False))
        .order_by(desc(RFIPdfProject.updated_at), desc(RFIPdfProject.created_at))
        .all()
    )
    for project in projects:
        _ensure_slug(db, project)
        _release_expired_lock(db, project)
    return [_project_payload(project, user) for project in projects]


@router.get("/list/mine")
async def list_my_rfi_pdfs(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if user.is_service_account:
        return []
    projects = (
        db.query(RFIPdfProject)
        .options(
            joinedload(RFIPdfProject.user),
            joinedload(RFIPdfProject.editing_user),
        )
        .filter(
            RFIPdfProject.user_id == user.id,
            RFIPdfProject.is_deleted.is_(False),
        )
        .order_by(desc(RFIPdfProject.updated_at), desc(RFIPdfProject.created_at))
        .all()
    )
    for project in projects:
        _ensure_slug(db, project)
        _release_expired_lock(db, project)
    return [_project_payload(project, user) for project in projects]


# ---------------------------------------------------------------------------
# Master data (sidebar feeds)
# ---------------------------------------------------------------------------


@router.get("/master-data/projects")
async def get_master_projects(
    search: str = Query("", description="Substring match against project name or code"),
    product: str = Query("", description="Substring match against linked product/brand/model"),
    years_back: int | None = Query(None, ge=1, le=20, description="Last N years filter"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return list_projects(
        db,
        search=search or None,
        product=product or None,
        years_back=years_back,
        limit=limit,
        offset=offset,
    )


@router.get("/master-data/engineers")
async def get_master_engineers(
    search: str = Query("", description="Substring match against engineer name or email"),
    role: str = Query("", description="Substring match against role labels (e.g. Engineer)"),
    min_experience_years: float | None = Query(
        None, ge=0, le=50, description="Lower bound for years of experience"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return list_engineers(
        db,
        search=search or None,
        role=role or None,
        min_experience_years=min_experience_years,
        limit=limit,
        offset=offset,
    )


@router.websocket("/ws/draft-stream/{document_key}")
async def ws_rfi_pdf_draft_stream(
    websocket: WebSocket,
    document_key: str,
    token: str = Query(""),
):
    """Relay live markdown chunks while Ollama streams the PDF RFI draft (see ``broadcast_pdf_draft``)."""
    user = await _websocket_current_user(token)
    if user is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    db = SessionLocal()
    try:
        proj = _get_project_by_key(db, document_key)
        if not proj:
            await websocket.close(code=4004, reason="RFI PDF not found")
            return

        pid = proj.id
        pst = proj.status
        pmd = proj.editor_markdown or ""
        errmsg = proj.error_message
        if not _pdf_project_readable_ws(user, proj):
            await websocket.close(code=4003, reason="Forbidden")
            return
    finally:
        db.close()

    await websocket.accept()
    await websocket.send_text(
        json.dumps({"type": "connected", "project_id": pid}, ensure_ascii=False)
    )

    if pst == STATUS_READY and pmd.strip():
        await websocket.send_text(json.dumps({"type": "already_complete"}, ensure_ascii=False))
        await websocket.close()
        return

    if pst == STATUS_FAILED:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "already_failed",
                    "message": errmsg or "Unknown failure",
                },
                ensure_ascii=False,
            )
        )
        await websocket.close()
        return

    subscriber_q = await subscribe_pdf_draft_stream(pid)
    try:
        while True:
            msg = await subscriber_q.get()
            await websocket.send_text(json.dumps(msg, ensure_ascii=False))
            if msg.get("type") in ("draft_complete", "draft_error"):
                break
    except WebSocketDisconnect:
        logger.info("RFI PDF draft WebSocket disconnected (project_id=%s)", pid)
    finally:
        await unsubscribe_pdf_draft_stream(pid, subscriber_q)
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Single project endpoints (placed AFTER master-data routes so /master-data
# is not treated as a document key)
# ---------------------------------------------------------------------------


@router.get("/{document_key}")
async def get_rfi_pdf(
    document_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    _release_expired_lock(db, project)
    return _project_payload(project, user)


@router.get("/{document_key}/timeline")
async def get_rfi_pdf_timeline(
    document_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    logs = (
        db.query(AuditLog)
        .options(joinedload(AuditLog.user))
        .filter(AuditLog.rfi_pdf_project_id == project.id)
        .order_by(desc(AuditLog.created_at))
        .all()
    )
    return [
        {
            "id": log.id,
            "user": _user_payload(log.user),
            "action": log.action,
            "resource_type": log.resource_type,
            "details": log.details or {},
            "created_at": iso_utc(log.created_at),
        }
        for log in logs
    ]


@router.post("/{document_key}/lock")
async def lock_rfi_pdf(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    _release_expired_lock(db, project)
    if project.editing_user_id and project.editing_user_id != user.id:
        holder_name = (
            project.editing_user.name if project.editing_user else "Another user"
        )
        raise HTTPException(
            status_code=409, detail=f"{holder_name} is still editing this RFI"
        )
    project.editing_user_id = user.id
    project.lock_acquired_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)
    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.lock_acquired",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={"filename": project.filename},
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.delete("/{document_key}/lock")
async def unlock_rfi_pdf(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    if (
        project.editing_user_id
        and project.editing_user_id != user.id
        and not user.is_admin
    ):
        holder_name = (
            project.editing_user.name if project.editing_user else "Another user"
        )
        raise HTTPException(
            status_code=409, detail=f"{holder_name} is still editing this RFI"
        )
    previous_editor = _user_payload(project.editing_user)
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    db.refresh(project)
    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.lock_released",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={"filename": project.filename, "previous_editor": previous_editor},
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.post("/{document_key}/save")
async def save_rfi_pdf(
    document_key: str,
    req: SaveDraftRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    _require_lock_owner(db, project, user)

    project.editor_markdown = req.editor_markdown
    project.editor_html = req.editor_html
    if req.entity_refs is not None:
        project.entity_refs = req.entity_refs
        flag_modified(project, "entity_refs")
    if req.metadata is not None:
        merged = {**(project.metadata_json or {}), **req.metadata}
        project.metadata_json = merged
        flag_modified(project, "metadata_json")
    project.updated_at = datetime.now(timezone.utc)
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.save",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={
            "filename": project.filename,
            "markdown_length": len(req.editor_markdown or ""),
            "entity_refs": len(req.entity_refs or []),
        },
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.post("/{document_key}/regenerate")
async def regenerate_rfi_pdf(
    document_key: str,
    req: RegenerateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    if not project.parsed_markdown:
        raise HTTPException(
            status_code=400,
            detail="The source PDF has not finished parsing yet",
        )
    if (
        project.editing_user_id
        and project.editing_user_id != user.id
        and not user.is_admin
    ):
        holder_name = (
            project.editing_user.name if project.editing_user else "Another user"
        )
        raise HTTPException(
            status_code=409, detail=f"{holder_name} is still editing this RFI"
        )

    project.status = STATUS_DRAFTING
    project.error_message = None
    project.editing_user_id = user.id if not user.is_service_account else None
    project.lock_acquired_at = datetime.now(timezone.utc)
    project.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.regenerate_started",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={
            "filename": project.filename,
            "model": req.model,
            "extra_instructions": (req.extra_instructions or "")[:500],
        },
        ip_address=_caller_ip(request),
    )

    async def _regenerate_task(project_id: int, model: str | None, instructions: str | None):
        await clear_pdf_draft_cancel(project_id)
        inner_db = SessionLocal()
        try:
            current = inner_db.query(RFIPdfProject).filter(RFIPdfProject.id == project_id).first()
            if not current or not current.parsed_markdown:
                return
            extraction = await extract_requirements(current.parsed_markdown, model=model)
            metadata = {
                "title": extraction.get("title"),
                "summary": extraction.get("summary"),
                "language": extraction.get("language"),
                "warning": extraction.get("warning"),
                "extra_instructions": instructions or None,
            }
            await broadcast_pdf_draft(project_id, {"type": "phase", "phase": "draft_started"})

            async def emit_delta(delta: str) -> None:
                await broadcast_pdf_draft(project_id, {"type": "draft_delta", "delta": delta})

            try:
                draft = await draft_response_markdown(
                    current.parsed_markdown,
                    extraction,
                    model=model,
                    on_stream_delta=emit_delta,
                    cancel_check=lambda: is_pdf_draft_cancelled(project_id),
                )
            except GenerationCancelled:
                logger.info("Regenerate cancelled for project %s", project_id)
                await broadcast_pdf_draft(project_id, {"type": "draft_complete"})
                return

            current.requirements = extraction.get("requirements", [])
            current.metadata_json = {**(current.metadata_json or {}), **metadata}
            current.editor_markdown = draft
            current.editor_html = None
            current.status = STATUS_READY
            current.editing_user_id = None
            current.lock_acquired_at = None
            current.updated_at = datetime.now(timezone.utc)
            flag_modified(current, "requirements")
            flag_modified(current, "metadata_json")
            inner_db.commit()
            await broadcast_pdf_draft(project_id, {"type": "draft_complete"})
            log_audit(
                inner_db,
                user_id=user.id if not user.is_service_account else None,
                action="rfi_pdf.regenerated",
                resource_type="rfi_pdf_project",
                rfi_pdf_project_id=project_id,
                details={
                    "filename": current.filename,
                    "requirements_count": len(extraction.get("requirements", [])),
                    "warning": extraction.get("warning"),
                },
                ip_address=_caller_ip(request),
            )
        except Exception as exc:
            logger.error("Regenerate failed: %s", exc, exc_info=True)
            try:
                await broadcast_pdf_draft(project_id, {"type": "draft_error", "message": str(exc)})
            except Exception:
                pass
            _update_status(
                inner_db,
                project_id,
                STATUS_FAILED,
                error=f"Regenerate failed: {exc}",
            )
            log_audit(
                inner_db,
                user_id=user.id if not user.is_service_account else None,
                action="rfi_pdf.regenerate_failed",
                resource_type="rfi_pdf_project",
                rfi_pdf_project_id=project_id,
                details={"error": str(exc)},
                ip_address=_caller_ip(request),
            )
        finally:
            inner_db.close()

    background_tasks.add_task(
        _regenerate_task,
        project.id,
        req.model,
        req.extra_instructions,
    )
    return _project_payload(project, user)


@router.post("/{document_key}/stop-generation")
async def stop_rfi_pdf_generation(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Cancel a running draft generation and keep partial content."""
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")

    # Set the cancellation flag — the streaming loop will pick this up
    await cancel_pdf_draft(project.id)

    # If the pipeline hasn't finished yet, mark the project as ready with
    # whatever content it has so far.
    if project.status in (STATUS_DRAFTING, STATUS_GENERATING, STATUS_EXTRACTING):
        project.status = STATUS_READY
        project.error_message = None
        project.editing_user_id = None
        project.lock_acquired_at = None
        project.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(project)

    # Tell any connected WebSocket clients that the draft is done
    await broadcast_pdf_draft(project.id, {"type": "draft_complete"})

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.generation_stopped",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={"filename": project.filename},
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)

@router.get("/{document_key}/preview")
async def preview_rfi_pdf(
    document_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    markdown = project.editor_markdown or ""
    if not markdown.strip():
        raise HTTPException(
            status_code=400,
            detail="No editor content available yet",
        )
    metadata = project.metadata_json or {}
    title = metadata.get("title") or project.filename or "RFI Response"
    try:
        pdf_bytes = render_pdf_bytes(
            markdown,
            title=str(title),
            footer=f"{project.filename or 'RFI'} · Preview",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{project.slug or project.id}_preview.pdf"',
        },
    )


@router.post("/{document_key}/export")
async def export_rfi_pdf(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    markdown = project.editor_markdown or ""
    if not markdown.strip():
        raise HTTPException(
            status_code=400,
            detail="No editor content available to export",
        )
    metadata = project.metadata_json or {}
    title = metadata.get("title") or project.filename or "RFI Response"
    try:
        pdf_bytes = render_pdf_bytes(
            markdown,
            title=str(title),
            footer=f"{project.filename or 'RFI'} · Final",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.export",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={"filename": project.filename, "bytes": len(pdf_bytes)},
        ip_address=_caller_ip(request),
    )

    download_name = (project.slug or f"rfi-{project.id}") + "_response.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
        },
    )


@router.delete("/{document_key}")
async def soft_delete_rfi_pdf(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, document_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFI PDF not found")
    if project.user_id and project.user_id != user.id and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to delete this RFI",
        )
    project.is_deleted = True
    project.deleted_at = datetime.now(timezone.utc)
    project.deleted_by_user_id = user.id if not user.is_service_account else None
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi_pdf.soft_delete",
        resource_type="rfi_pdf_project",
        rfi_pdf_project_id=project.id,
        details={"filename": project.filename, "slug": project.slug},
        ip_address=_caller_ip(request),
    )
    return {
        "status": "deleted",
        "documentId": project.slug or str(project.id),
    }
