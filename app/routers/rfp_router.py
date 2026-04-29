import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from app.config import OLLAMA_API, OLLAMA_MODEL
from app.core.security import CurrentUser, get_current_user, verify_bearer_any
from app.core.time import iso_utc
from app.db.database import AuditLog, RFPProject, SessionLocal, User, get_db
from app.schemas.rfp_schema import GenerateTechnicalContentRequest
from app.services.audit_service import log_audit
from app.services.rfp.generator import stream_technical_content, stream_adjust_content

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rfp", tags=["RFP"])
LOCK_TIMEOUT = timedelta(minutes=30)


class CreateRFPProjectRequest(BaseModel):
    product: str
    project_name: Optional[str] = None
    project_description: Optional[str] = None


class SaveRFPProjectRequest(BaseModel):
    content: str
    chat_messages: Optional[list[dict[str, Any]]] = None


class AppendRFPChatRequest(BaseModel):
    role: str
    content: str


class QueueRFPGenerationRequest(BaseModel):
    adjust: bool = False
    content: Optional[str] = None
    additionalContext: Optional[str] = None


def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "rfp"


def _unique_project_slug(db: Session, base: str, exclude_project_id: int | None = None) -> str:
    base_slug = _slugify(base)
    candidate = base_slug
    suffix = 1
    while True:
        query = db.query(RFPProject).filter(RFPProject.slug == candidate)
        if exclude_project_id is not None:
            query = query.filter(RFPProject.id != exclude_project_id)
        if query.first() is None:
            return candidate
        candidate = f"{base_slug}-{suffix}"
        suffix += 1


def _ensure_project_slug(db: Session, project: RFPProject) -> RFPProject:
    if project.slug:
        return project
    project.slug = _unique_project_slug(
        db,
        project.project_name or f"{project.product}-chapter-3-{project.id}",
        exclude_project_id=project.id,
    )
    db.commit()
    db.refresh(project)
    return project


def _get_project_by_key(
    db: Session,
    project_key: str,
    *,
    include_deleted: bool = False,
) -> RFPProject | None:
    query = db.query(RFPProject).options(
        joinedload(RFPProject.user),
        joinedload(RFPProject.editing_user),
    )
    if not include_deleted:
        query = query.filter(RFPProject.is_deleted.is_(False))
    project = None
    if project_key.isdigit():
        project = query.filter(RFPProject.id == int(project_key)).first()
    if project is None:
        project = query.filter(RFPProject.slug == project_key).first()
    if project is not None:
        project = _ensure_project_slug(db, project)
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


def _is_lock_expired(project: RFPProject) -> bool:
    if not project.editing_user_id or not project.lock_acquired_at:
        return False
    lock_time = project.lock_acquired_at
    if lock_time.tzinfo is None:
        lock_time = lock_time.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - lock_time > LOCK_TIMEOUT


def _release_expired_lock(db: Session, project: RFPProject) -> bool:
    if not _is_lock_expired(project):
        return False
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    db.refresh(project)
    return True


def _require_lock_owner(db: Session, project: RFPProject, user: CurrentUser):
    _release_expired_lock(db, project)
    if project.editing_user_id != user.id:
        holder_name = project.editing_user.name if project.editing_user else "Another user"
        raise HTTPException(status_code=409, detail=f"{holder_name} is still updating the file")


def _project_payload(project: RFPProject, current_user: CurrentUser | None = None) -> dict:
    locked_by_other = bool(
        current_user
        and project.editing_user_id
        and project.editing_user_id != current_user.id
    )
    return {
        "documentId": project.slug or str(project.id),
        "id": project.id,
        "slug": project.slug,
        "product": project.product,
        "project_name": project.project_name,
        "project_description": project.project_description,
        "content": project.content or "",
        "chat_messages": project.chat_messages or [],
        "status": project.status,
        "created_at": iso_utc(project.created_at),
        "updated_at": iso_utc(project.updated_at or project.created_at),
        "user": _user_payload(project.user),
        "editing_user": _user_payload(project.editing_user),
        "lock_acquired_at": iso_utc(project.lock_acquired_at),
        "is_locked_by_other": locked_by_other,
        "is_lock_held_by_me": bool(current_user and project.editing_user_id == current_user.id),
    }


def _simple_markdown_to_html(content: str) -> str:
    """Small fallback renderer for background jobs.

    The live browser path uses the frontend markdown renderer. Background jobs
    run without a browser, so save readable HTML instead of losing the result.
    """
    blocks: list[str] = []
    for raw_block in re.split(r"\n\s*\n", content.strip()):
        block = raw_block.strip()
        if not block:
            continue
        escaped = html.escape(block).replace("\n", "<br />")
        blocks.append(f"<p>{escaped}</p>")
    return "\n".join(blocks)


async def _run_rfp_generation_background(
    project_id: int,
    *,
    user_id: Optional[int],
    ip_address: Optional[str],
    adjust: bool,
    content: Optional[str],
    additional_context: Optional[str],
):
    db = SessionLocal()
    try:
        project = (
            db.query(RFPProject)
            .options(joinedload(RFPProject.user))
            .filter(RFPProject.id == project_id, RFPProject.is_deleted.is_(False))
            .first()
        )
        if not project:
            return

        generator = (
            stream_adjust_content(
                product=project.product,
                content=content or project.content or "",
                additional_context=additional_context,
            )
            if adjust
            else stream_technical_content(product=project.product)
        )

        full_content = ""
        warnings: list[str] = []
        async for message in generator:
            message_type = message.get("type")
            if message_type == "chunk":
                full_content += str(message.get("content") or "")
            elif message_type == "warning":
                warnings.append(str(message.get("message") or ""))
            elif message_type == "complete":
                full_content = str(message.get("fullContent") or full_content)
            elif message_type == "error":
                project.status = "failed"
                project.updated_at = datetime.now(timezone.utc)
                project.editing_user_id = None
                project.lock_acquired_at = None
                db.commit()
                log_audit(
                    db,
                    user_id=user_id,
                    action="rfp.background_generate_failed",
                    resource_type="rfp_project",
                    rfp_project_id=project.id,
                    details={
                        "product": project.product,
                        "project_name": project.project_name,
                        "message": message.get("message"),
                    },
                    ip_address=ip_address,
                )
                return

        if full_content.strip():
            messages = list(project.chat_messages or [])
            messages.append(
                {
                    "role": "assistant",
                    "content": (
                        "Background generation completed "
                        f"({len(full_content):,} characters)"
                    ),
                    "user": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            project.content = _simple_markdown_to_html(full_content)
            project.chat_messages = messages
            project.status = "completed"
            project.updated_at = datetime.now(timezone.utc)
            project.editing_user_id = None
            project.lock_acquired_at = None
            flag_modified(project, "chat_messages")
            db.commit()

            log_audit(
                db,
                user_id=user_id,
                action="rfp.background_generate_completed",
                resource_type="rfp_project",
                rfp_project_id=project.id,
                details={
                    "product": project.product,
                    "project_name": project.project_name,
                    "adjust": adjust,
                    "warnings": warnings,
                },
                ip_address=ip_address,
            )
    except Exception as exc:
        logger.error(f"Background RFP generation failed: {exc}", exc_info=True)
        project = db.query(RFPProject).filter(RFPProject.id == project_id).first()
        if project:
            project.status = "failed"
            project.updated_at = datetime.now(timezone.utc)
            project.editing_user_id = None
            project.lock_acquired_at = None
            db.commit()
    finally:
        db.close()


@router.post("/projects")
async def create_rfp_project(
    req: CreateRFPProjectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    product = req.product.strip()
    if not product:
        raise HTTPException(status_code=422, detail="Product is required")

    project_name = (req.project_name or f"{product} Chapter 3").strip()
    existing = (
        db.query(RFPProject)
        .options(joinedload(RFPProject.user), joinedload(RFPProject.editing_user))
        .filter(
            func.lower(RFPProject.product) == product.lower(),
            func.lower(RFPProject.project_name) == project_name.lower(),
            RFPProject.is_deleted.is_(False),
        )
        .first()
    )
    if existing:
        payload = _project_payload(_ensure_project_slug(db, existing), user)
        payload["created"] = False
        return payload

    project = RFPProject(
        product=product,
        project_name=project_name,
        project_description=req.project_description,
        slug=_unique_project_slug(db, project_name or product),
        status="draft",
        user_id=user.id if not user.is_service_account else None,
        chat_messages=[],
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.create",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={"product": product, "project_name": project_name},
        ip_address=_caller_ip(request),
    )
    payload = _project_payload(project, user)
    payload["created"] = True
    return payload


@router.get("/list")
async def list_rfp_projects(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    projects = (
        db.query(RFPProject)
        .options(joinedload(RFPProject.user), joinedload(RFPProject.editing_user))
        .filter(RFPProject.is_deleted.is_(False))
        .order_by(desc(RFPProject.updated_at), desc(RFPProject.created_at))
        .all()
    )
    for project in projects:
        _ensure_project_slug(db, project)
        _release_expired_lock(db, project)
    return [_project_payload(project, user) for project in projects]


@router.get("/list/mine")
async def list_my_rfp_projects(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if user.is_service_account:
        return []
    projects = (
        db.query(RFPProject)
        .options(joinedload(RFPProject.user), joinedload(RFPProject.editing_user))
        .filter(RFPProject.user_id == user.id, RFPProject.is_deleted.is_(False))
        .order_by(desc(RFPProject.updated_at), desc(RFPProject.created_at))
        .all()
    )
    for project in projects:
        _ensure_project_slug(db, project)
        _release_expired_lock(db, project)
    return [_project_payload(project, user) for project in projects]


@router.get("/projects/{project_key}")
async def get_rfp_project(
    project_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    _release_expired_lock(db, project)
    return _project_payload(project, user)


@router.post("/projects/{project_key}/lock")
async def lock_rfp_project(
    project_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    _release_expired_lock(db, project)
    if project.editing_user_id and project.editing_user_id != user.id:
        holder_name = project.editing_user.name if project.editing_user else "Another user"
        raise HTTPException(status_code=409, detail=f"{holder_name} is still updating the file")

    project.editing_user_id = user.id
    project.lock_acquired_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.lock_acquired",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={"product": project.product, "project_name": project.project_name},
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.delete("/projects/{project_key}/lock")
async def unlock_rfp_project(
    project_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    if project.editing_user_id and project.editing_user_id != user.id and not user.is_admin:
        holder_name = project.editing_user.name if project.editing_user else "Another user"
        raise HTTPException(status_code=409, detail=f"{holder_name} is still updating the file")

    previous_editor = _user_payload(project.editing_user)
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.lock_released",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={"product": project.product, "project_name": project.project_name, "previous_editor": previous_editor},
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.post("/projects/{project_key}/save")
async def save_rfp_project(
    project_key: str,
    req: SaveRFPProjectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    _require_lock_owner(db, project, user)

    project.content = req.content
    if req.chat_messages is not None:
        project.chat_messages = req.chat_messages
        flag_modified(project, "chat_messages")
    project.status = "completed" if req.content.strip() else "draft"
    project.updated_at = datetime.now(timezone.utc)
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.save",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={"product": project.product, "project_name": project.project_name},
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.post("/projects/{project_key}/chat")
async def append_rfp_chat(
    project_key: str,
    req: AppendRFPChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    if req.role not in {"user", "assistant", "system"}:
        raise HTTPException(status_code=422, detail="role must be user, assistant, or system")
    if not req.content.strip():
        raise HTTPException(status_code=422, detail="content is required")

    messages = list(project.chat_messages or [])
    entry = {
        "role": req.role,
        "content": req.content.strip(),
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
        } if req.role == "user" else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    messages.append(entry)
    project.chat_messages = messages
    project.updated_at = datetime.now(timezone.utc)
    flag_modified(project, "chat_messages")
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.prompt" if req.role == "user" else "rfp.assistant",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={
            "role": req.role,
            "content": req.content.strip()[:500],
            "product": project.product,
            "project_name": project.project_name,
        },
        ip_address=_caller_ip(request),
    )
    return _project_payload(project, user)


@router.post("/projects/{project_key}/generate-background")
async def queue_rfp_generation_background(
    project_key: str,
    req: QueueRFPGenerationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    if req.adjust and not (req.content or "").strip():
        raise HTTPException(status_code=422, detail="content is required when adjust=true")
    if project.editing_user_id and project.editing_user_id != user.id and not user.is_admin:
        holder_name = project.editing_user.name if project.editing_user else "Another user"
        raise HTTPException(status_code=409, detail=f"{holder_name} is still updating the file")

    project.status = "generating"
    project.editing_user_id = user.id if not user.is_service_account else None
    project.lock_acquired_at = datetime.now(timezone.utc)
    project.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.background_generate_started",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={
            "product": project.product,
            "project_name": project.project_name,
            "adjust": req.adjust,
            "additionalContext": (req.additionalContext or "")[:500],
        },
        ip_address=_caller_ip(request),
    )

    background_tasks.add_task(
        _run_rfp_generation_background,
        project.id,
        user_id=user.id if not user.is_service_account else None,
        ip_address=_caller_ip(request),
        adjust=req.adjust,
        content=req.content,
        additional_context=req.additionalContext,
    )
    return _project_payload(project, user)


@router.get("/projects/{project_key}/timeline")
async def get_rfp_timeline(
    project_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    logs = (
        db.query(AuditLog)
        .options(joinedload(AuditLog.user))
        .filter(AuditLog.rfp_project_id == project.id)
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


@router.delete("/projects/{project_key}")
async def soft_delete_rfp_project(
    project_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    project = _get_project_by_key(db, project_key)
    if not project:
        raise HTTPException(status_code=404, detail="RFP project not found")
    if project.user_id and project.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this RFP")

    project.is_deleted = True
    project.deleted_at = datetime.now(timezone.utc)
    project.deleted_by_user_id = user.id if not user.is_service_account else None
    project.editing_user_id = None
    project.lock_acquired_at = None
    db.commit()

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfp.soft_delete",
        resource_type="rfp_project",
        rfp_project_id=project.id,
        details={"product": project.product, "project_name": project.project_name, "slug": project.slug},
        ip_address=_caller_ip(request),
    )
    return {"status": "deleted", "documentId": project.slug or str(project.id)}


@router.websocket("/ws/generate-technical")
async def ws_generate_technical(
    websocket: WebSocket,
    token: str = Query(default=""),
):
    """WebSocket endpoint for streaming RFP Chapter 3 generation + adjustment.

    Flow:
      1. Client connects with ?token=<bearer_token>
      2. Client sends JSON: {"product": "...", "rfp": true}
      3. Server streams chunks → complete
      4. Client can then send: {"product": "...", "rfp": true, "adjust": true, "content": "...", "additionalContext": "make it shorter"}
      5. Server streams adjusted content → complete
      6. Repeat step 4-5 as many times as needed
      7. Client disconnects when done
    """
    if not await verify_bearer_any(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    logger.info("WebSocket client connected for RFP generation")

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                payload = json.loads(raw)
                request = GenerateTechnicalContentRequest(**payload)
            except (json.JSONDecodeError, Exception) as e:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Invalid request payload: {str(e)}",
                }))
                continue

            if not request.rfp:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "rfp must be true for RFP generation",
                }))
                continue

            # ── Adjust mode ───────────────────────────────────────────
            if request.adjust:
                if not request.content:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "content is required when adjust=true",
                    }))
                    continue

                logger.info(f"Adjusting RFP content for product: {request.product}")

                async for message in stream_adjust_content(
                    product=request.product,
                    content=request.content,
                    additional_context=request.additionalContext,
                ):
                    await websocket.send_text(json.dumps(message, ensure_ascii=False))
                    if message.get("type") == "error":
                        break

            # ── Generate mode ─────────────────────────────────────────
            else:
                logger.info(f"Generating RFP technical content for product: {request.product}")

                async for message in stream_technical_content(
                    product=request.product,
                    project_name=request.projectName,
                    project_description=request.projectDescription,
                    additional_context=request.additionalContext,
                ):
                    await websocket.send_text(json.dumps(message, ensure_ascii=False))
                    if message.get("type") == "error":
                        break

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Server error: {str(e)}",
            }))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _classify_prompt_with_ollama(prompt: str) -> dict:
    """Use Ollama to classify if a prompt is vague or specific."""
    import httpx
    import re

    system_prompt = (
        "You are an assistant that classifies user prompts for RFP document editing. "
        "Determine if the prompt is 'vague' or 'specific'.\n\n"
        "Vague: ONLY single emotional words with zero context ('too bad', 'ugly', 'not good').\n\n"
        "Specific: ANY prompt that mentions a section/topic, language, length, format, or action. "
        "Examples: 'do it', 'migration', 'semuanya', 'make it shorter', 'make it in korean', "
        "'all of them', 'make it in points', 'too long' are ALL specific when context exists.\n\n"
        "CRITICAL RULES:\n"
        "1. If the prompt contains ANY technical term, section name, or action word → classify as 'specific'.\n"
        "2. If the prompt is a command like 'do it', 'apply', 'execute', 'yes', 'ok' → classify as 'specific'.\n"
        "3. Single words like 'semuanya', 'all', 'migration', 'security' in a follow-up context → 'specific'.\n"
        "4. ONLY classify as 'vague' if there is truly zero actionable information AND no prior context exists.\n"
        "5. When in doubt, classify as 'specific' and execute.\n\n"
        "If vague (rare), generate exactly ONE (1) short clarifying question. "
        "Use ENGLISH or INDONESIAN matching the user's language.\n\n"
        "Respond ONLY with JSON: {\"classification\": \"vague\"|\"specific\", \"questions\": []}"
    )

    user_prompt = f"Classify this prompt:\n\n\"{prompt}\""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": user_prompt,
        "system": system_prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 256,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info(f"Calling Ollama at {OLLAMA_API}/api/generate with model {OLLAMA_MODEL}")
            response = await client.post(f"{OLLAMA_API}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            result_text = data.get("response", "")
            logger.info(f"Ollama response: {result_text[:200]}")

            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            logger.warning("No JSON found in Ollama response")
            return {"classification": "vague", "questions": ["Can you specify what needs improvement?"]}
    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to Ollama at {OLLAMA_API}: {e}")
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}
    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama HTTP error: {e.response.status_code} - {e.response.text}")
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Ollama JSON response: {e}")
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}
    except Exception as e:
        logger.error(f"Unexpected error classifying prompt: {e}", exc_info=True)
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}


@router.post("/classify-adjust-prompt")
async def classify_adjust_prompt(body: dict):
    """Classify a user's adjustment prompt as vague or specific.

    Returns:
        - classification: "vague" or "specific"
        - questions: list of clarifying questions (if vague)
    """
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    result = await _classify_prompt_with_ollama(prompt)
    return JSONResponse(content=result)
