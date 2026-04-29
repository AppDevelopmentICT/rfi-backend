from fastapi import APIRouter, File, HTTPException, UploadFile, Depends, Request
from sqlalchemy.orm import Session

from app.services.rfi.core import parse_excel_bytes, flatten_sheets_to_questions
from app.services.knowledge.storage import document_store
from app.schemas.ai_schema import UploadDocumentResponse
from app.core.security import get_current_user, CurrentUser
from app.db.database import get_db
from app.services.audit_service import log_audit

router = APIRouter(prefix="/api/v1/document", tags=["Document"])


def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/upload", response_model=UploadDocumentResponse)
async def upload_document(
    request: Request,
    file: UploadFile = File(..., description="The .xlsx file to parse"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=422,
            detail="Only .xlsx or .xls files are accepted",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Uploaded file is empty")

    try:
        sheets_data = parse_excel_bytes(file_bytes)

        questions = flatten_sheets_to_questions(sheets_data)

        doc_id = document_store.save_document(sheets_data, questions)

        log_audit(
            db,
            user_id=user.id if not user.is_service_account else None,
            action="document.upload_excel",
            resource_type="excel",
            details={"filename": file.filename, "stored_id": doc_id},
            ip_address=_caller_ip(request),
        )

        return {
            "documentId": doc_id,
            "questions": questions
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to process document: {e}")
