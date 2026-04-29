from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import desc
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
import openpyxl

from app.config import OLLAMA_MODEL
from app.services.rfi.core import parse_excel_bytes, auto_fill_bytes
from app.schemas.excel_schema import ErrorResponse
from app.core.time import iso_utc
from app.db.database import AuditLog, RFIProject, SessionLocal, User, get_db
from app.core.security import get_current_user, CurrentUser
from app.services.audit_service import log_audit
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/rfi", tags=["RFI/RFP"])
LOCK_TIMEOUT = timedelta(minutes=30)

def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _split_filename(filename: str) -> tuple[str, str]:
    if "." not in filename:
        return filename, ""
    stem, extension = filename.rsplit(".", 1)
    return stem, f".{extension}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "document"


def _unique_document_identity(
    db: Session,
    filename: str,
    *,
    exclude_project_id: int | None = None,
) -> tuple[str, str]:
    stem, extension = _split_filename(filename)
    extension = extension or ".xlsx"
    base_slug = _slugify(stem)
    candidate_slug = base_slug
    candidate_stem = stem
    suffix = 1

    while True:
        query = db.query(RFIProject).filter(RFIProject.slug == candidate_slug)
        if exclude_project_id is not None:
            query = query.filter(RFIProject.id != exclude_project_id)
        if query.first() is None:
            return f"{candidate_stem}{extension}", candidate_slug
        candidate_slug = f"{base_slug}-{suffix}"
        candidate_stem = f"{stem}-{suffix}"
        suffix += 1


def _ensure_project_slug(db: Session, doc: RFIProject) -> RFIProject:
    if doc.slug:
        return doc
    doc.filename, doc.slug = _unique_document_identity(
        db,
        doc.filename or f"document-{doc.id}.xlsx",
        exclude_project_id=doc.id,
    )
    db.commit()
    db.refresh(doc)
    return doc


def _get_project_by_key(
    db: Session,
    document_key: str,
    *,
    include_deleted: bool = False,
) -> RFIProject | None:
    query = db.query(RFIProject).options(
        joinedload(RFIProject.user),
        joinedload(RFIProject.editing_user),
    )
    if not include_deleted:
        query = query.filter(RFIProject.is_deleted.is_(False))
    doc = None
    if document_key.isdigit():
        doc = query.filter(RFIProject.id == int(document_key)).first()
    if doc is None:
        doc = query.filter(RFIProject.slug == document_key).first()
    if doc is not None:
        doc = _ensure_project_slug(db, doc)
    return doc


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


def _is_lock_expired(doc: RFIProject) -> bool:
    if not doc.editing_user_id or not doc.lock_acquired_at:
        return False
    lock_time = doc.lock_acquired_at
    if lock_time.tzinfo is None:
        lock_time = lock_time.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - lock_time > LOCK_TIMEOUT


def _release_expired_lock(db: Session, doc: RFIProject) -> bool:
    if not _is_lock_expired(doc):
        return False
    doc.editing_user_id = None
    doc.lock_acquired_at = None
    db.commit()
    db.refresh(doc)
    return True


def _require_lock_owner(db: Session, doc: RFIProject, user: CurrentUser):
    _release_expired_lock(db, doc)
    if doc.editing_user_id != user.id:
        holder_name = doc.editing_user.name if doc.editing_user else "Another user"
        raise HTTPException(
            status_code=409,
            detail=f"{holder_name} is still updating the file",
        )


def _project_payload(doc: RFIProject, current_user: CurrentUser | None = None) -> dict:
    locked_by_other = bool(
        current_user
        and doc.editing_user_id
        and doc.editing_user_id != current_user.id
    )
    return {
        "documentId": doc.slug or str(doc.id),
        "id": doc.id,
        "slug": doc.slug,
        "fileName": doc.filename,
        "filename": doc.filename,
        "excelData": doc.json_data,
        "status": doc.status,
        "created_at": iso_utc(doc.created_at),
        "updated_at": iso_utc(doc.updated_at or doc.created_at),
        "user": _user_payload(doc.user),
        "uploaded_by": _user_payload(doc.user),
        "editing_user": _user_payload(doc.editing_user),
        "lock_acquired_at": iso_utc(doc.lock_acquired_at),
        "is_locked_by_other": locked_by_other,
        "is_lock_held_by_me": bool(current_user and doc.editing_user_id == current_user.id),
    }

@router.post("/upload-and-read")
async def upload_and_read_rfi(
    request: Request,
    file: UploadFile = File(...),
):
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=422, detail="Only .xlsx or .xls files are accepted")
    
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Uploaded file is empty")

    try:
        json_data = parse_excel_bytes(file_bytes)
        temp_id = str(uuid.uuid4())
        
        return {"documentId": temp_id, "fileName": file.filename, "excelData": json_data}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to parse Excel: {e}")

@router.get("/list")
async def list_rfi_documents(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    docs = (
        db.query(RFIProject)
        .options(joinedload(RFIProject.user), joinedload(RFIProject.editing_user))
        .filter(RFIProject.is_deleted.is_(False))
        .order_by(desc(RFIProject.updated_at), desc(RFIProject.created_at))
        .all()
    )
    for doc in docs:
        _ensure_project_slug(db, doc)
        _release_expired_lock(db, doc)
    return [_project_payload(doc, user) for doc in docs]


@router.get("/list/mine")
async def list_my_rfi_documents(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if user.is_service_account:
        return []
    docs = (
        db.query(RFIProject)
        .options(joinedload(RFIProject.user), joinedload(RFIProject.editing_user))
        .filter(RFIProject.user_id == user.id, RFIProject.is_deleted.is_(False))
        .order_by(desc(RFIProject.updated_at), desc(RFIProject.created_at))
        .all()
    )
    for doc in docs:
        _ensure_project_slug(db, doc)
        _release_expired_lock(db, doc)
    return [_project_payload(doc, user) for doc in docs]


@router.get("/{document_key}")
async def get_rfi_document(
    document_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    _release_expired_lock(db, doc)
    return _project_payload(doc, user)


async def run_autofill_task(
    project_id: int,
    file_bytes: bytes,
    original_filename: str,
    model: Optional[str],
    ctx_cols: Optional[list],
    fill_cols: Optional[list],
    user_id: int,
    ip_address: str
):
    db = SessionLocal()
    try:
        result = await auto_fill_bytes(
            file_bytes,
            model=model,
            context_columns=ctx_cols,
            fill_columns=fill_cols,
        )
        if result["results"]:
            generated_json = parse_excel_bytes(result["filled_bytes"])
            project = db.query(RFIProject).filter(RFIProject.id == project_id).first()
            if project:
                project.json_data = generated_json
                project.status = "completed"
                project.updated_at = datetime.now(timezone.utc)
                flag_modified(project, "json_data")
                db.commit()

                log_audit(
                    db,
                    user_id=user_id,
                    action="rfi.autofill",
                    resource_type="rfi_project",
                    rfi_project_id=project_id,
                    details={"generated_id": project_id, "filename": project.filename},
                    ip_address=ip_address,
                )
        else:
            project = db.query(RFIProject).filter(RFIProject.id == project_id).first()
            if project:
                project.status = "failed"
                project.json_data = {"error": "No empty cells found to fill."}
                project.updated_at = datetime.now(timezone.utc)
                db.commit()
    except Exception as e:
        project = db.query(RFIProject).filter(RFIProject.id == project_id).first()
        if project:
            project.status = "failed"
            project.json_data = {"error": str(e)}
            project.updated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()

@router.post("/auto-fill")
async def autofill_rfi_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="The .xlsx file to auto-fill"),
    model: Optional[str] = Form(default=None),
    context_columns: Optional[str] = Form(default=None),
    fill_columns: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Uploaded file is empty")

    ctx_cols = [c.strip() for c in context_columns.split(",") if c.strip()] if context_columns else None
    fill_cols = [c.strip() for c in fill_columns.split(",") if c.strip()] if fill_columns else None
    
    filename, slug = _unique_document_identity(
        db,
        file.filename.rsplit(".", 1)[0] + "_answered.xlsx",
    )

    gen_doc = RFIProject(
        filename=filename,
        slug=slug,
        status="generating",
        user_id=user.id if not user.is_service_account else None
    )
    db.add(gen_doc)
    db.commit()
    db.refresh(gen_doc)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.generate_started",
        resource_type="rfi_project",
        rfi_project_id=gen_doc.id,
        details={"filename": gen_doc.filename, "source_filename": file.filename},
        ip_address=_caller_ip(request),
    )

    background_tasks.add_task(
        run_autofill_task,
        gen_doc.id,
        file_bytes,
        file.filename,
        model,
        ctx_cols,
        fill_cols,
        user.id if not user.is_service_account else None,
        _caller_ip(request)
    )

    return {"documentId": gen_doc.slug, "status": "generating", "fileName": gen_doc.filename}

class UpdateCellRequest(BaseModel):
    sheet: str
    rowIdx: int
    column: str
    value: str


class SaveRfiRequest(BaseModel):
    excelData: dict


@router.post("/{document_key}/lock")
async def lock_rfi_document(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    _release_expired_lock(db, doc)
    if doc.editing_user_id and doc.editing_user_id != user.id:
        holder_name = doc.editing_user.name if doc.editing_user else "Another user"
        raise HTTPException(
            status_code=409,
            detail=f"{holder_name} is still updating the file",
        )

    doc.editing_user_id = user.id
    doc.lock_acquired_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(doc)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.lock_acquired",
        resource_type="rfi",
        rfi_project_id=doc.id,
        details={"filename": doc.filename},
        ip_address=_caller_ip(request),
    )
    return _project_payload(doc, user)


@router.delete("/{document_key}/lock")
async def unlock_rfi_document(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.editing_user_id and doc.editing_user_id != user.id and not user.is_admin:
        holder_name = doc.editing_user.name if doc.editing_user else "Another user"
        raise HTTPException(
            status_code=409,
            detail=f"{holder_name} is still updating the file",
        )

    previous_editor = _user_payload(doc.editing_user)
    doc.editing_user_id = None
    doc.lock_acquired_at = None
    db.commit()
    db.refresh(doc)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.lock_released",
        resource_type="rfi",
        rfi_project_id=doc.id,
        details={"filename": doc.filename, "previous_editor": previous_editor},
        ip_address=_caller_ip(request),
    )
    return _project_payload(doc, user)

@router.put("/{document_key}/update-cell")
async def update_rfi_cell(
    document_key: str,
    req: UpdateCellRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    _require_lock_owner(db, doc, user)
    
    json_data = doc.json_data
    if not json_data or req.sheet not in json_data:
        raise HTTPException(status_code=404, detail="Sheet not found")
    
    sheet_data = json_data[req.sheet]["data"]
    if req.rowIdx < 0 or req.rowIdx >= len(sheet_data):
        raise HTTPException(status_code=400, detail="Invalid row index")
    
    sheet_data[req.rowIdx][req.column] = req.value
    doc.json_data = json_data
    doc.updated_at = datetime.now(timezone.utc)
    flag_modified(doc, "json_data")
    db.commit()
    
    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.update_cell",
        resource_type="rfi",
        rfi_project_id=doc.id,
        details={"sheet": req.sheet, "rowIdx": req.rowIdx, "column": req.column},
        ip_address=_caller_ip(request),
    )

    return {"status": "ok"}


@router.post("/{document_key}/save")
async def save_rfi_document(
    document_key: str,
    req: SaveRfiRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    _require_lock_owner(db, doc, user)

    doc.json_data = req.excelData
    doc.updated_at = datetime.now(timezone.utc)
    doc.editing_user_id = None
    doc.lock_acquired_at = None
    flag_modified(doc, "json_data")
    db.commit()
    db.refresh(doc)

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.save",
        resource_type="rfi",
        rfi_project_id=doc.id,
        details={"filename": doc.filename},
        ip_address=_caller_ip(request),
    )
    return _project_payload(doc, user)


@router.get("/{document_key}/timeline")
async def get_rfi_timeline(
    document_key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    logs = (
        db.query(AuditLog)
        .options(joinedload(AuditLog.user))
        .filter(AuditLog.rfi_project_id == doc.id)
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

@router.get("/{document_key}/download")
async def download_rfi(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    if doc.status != "completed" or not doc.json_data:
        raise HTTPException(status_code=400, detail="Document is not ready for download")
    
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    
    for sheet_name, sheet_content in doc.json_data.items():
        if sheet_name == "error":
            continue
        ws = wb.create_sheet(title=sheet_name[:31])
        headers = sheet_content.get("headers", [])
        data = sheet_content.get("data", [])
        ws.append(headers)
        for row in data:
            row_values = [row.get(h, "") for h in headers]
            ws.append(row_values)
            
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.export",
        resource_type="rfi",
        rfi_project_id=doc.id,
        details={"filename": doc.filename},
        ip_address=_caller_ip(request),
    )

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'}
    )


@router.delete("/{document_key}")
async def soft_delete_rfi_document(
    document_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = _get_project_by_key(db, document_key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.user_id and doc.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this document")

    doc.is_deleted = True
    doc.deleted_at = datetime.now(timezone.utc)
    doc.deleted_by_user_id = user.id if not user.is_service_account else None
    doc.editing_user_id = None
    doc.lock_acquired_at = None
    db.commit()

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.soft_delete",
        resource_type="rfi_project",
        rfi_project_id=doc.id,
        details={"filename": doc.filename, "slug": doc.slug},
        ip_address=_caller_ip(request),
    )
    return {"status": "deleted", "documentId": doc.slug or str(doc.id)}
