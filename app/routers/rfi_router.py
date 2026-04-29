from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
import io
import uuid
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
import openpyxl

from app.config import OLLAMA_MODEL
from app.services.rfi.core import parse_excel_bytes, auto_fill_bytes
from app.schemas.excel_schema import ErrorResponse
from app.db.database import get_db, RFIProject, SessionLocal
from app.core.security import get_current_user, CurrentUser
from app.services.audit_service import log_audit
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/rfi", tags=["RFI/RFP"])

def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None

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

@router.get("/{document_id}")
async def get_rfi_document(
    document_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = db.query(RFIProject).filter(RFIProject.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"documentId": str(doc.id), "fileName": doc.filename, "excelData": doc.json_data, "status": doc.status}


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
                flag_modified(project, "json_data")
                db.commit()

                log_audit(
                    db,
                    user_id=user_id,
                    action="rfi.autofill",
                    resource_type="rfi_project",
                    document_id=project_id,
                    details={"generated_id": project_id},
                    ip_address=ip_address,
                )
        else:
            project = db.query(RFIProject).filter(RFIProject.id == project_id).first()
            if project:
                project.status = "failed"
                project.json_data = {"error": "No empty cells found to fill."}
                db.commit()
    except Exception as e:
        project = db.query(RFIProject).filter(RFIProject.id == project_id).first()
        if project:
            project.status = "failed"
            project.json_data = {"error": str(e)}
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
    
    gen_doc = RFIProject(
        filename=file.filename.rsplit(".", 1)[0] + "_answered.xlsx",
        status="generating",
        user_id=user.id if not user.is_service_account else None
    )
    db.add(gen_doc)
    db.commit()
    db.refresh(gen_doc)

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

    return {"documentId": str(gen_doc.id), "status": "generating"}

class UpdateCellRequest(BaseModel):
    sheet: str
    rowIdx: int
    column: str
    value: str

@router.put("/{document_id}/update-cell")
async def update_rfi_cell(
    document_id: int,
    req: UpdateCellRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = db.query(RFIProject).filter(RFIProject.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    json_data = doc.json_data
    if not json_data or req.sheet not in json_data:
        raise HTTPException(status_code=404, detail="Sheet not found")
    
    sheet_data = json_data[req.sheet]["data"]
    if req.rowIdx < 0 or req.rowIdx >= len(sheet_data):
        raise HTTPException(status_code=400, detail="Invalid row index")
    
    sheet_data[req.rowIdx][req.column] = req.value
    doc.json_data = json_data
    flag_modified(doc, "json_data")
    db.commit()
    
    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="rfi.update_cell",
        resource_type="rfi",
        document_id=doc.id,
        details={"sheet": req.sheet, "rowIdx": req.rowIdx, "column": req.column},
        ip_address=_caller_ip(request),
    )

    return {"status": "ok"}

@router.get("/{document_id}/download")
async def download_rfi(
    document_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = db.query(RFIProject).filter(RFIProject.id == document_id).first()
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
        document_id=doc.id,
        details={"filename": doc.filename},
        ip_address=_caller_ip(request),
    )

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'}
    )
