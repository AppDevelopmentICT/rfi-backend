from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from app.services.rfi_service import parse_excel_bytes, flatten_sheets_to_questions
from app.services.document_store import document_store
from app.schemas.ai_schema import UploadDocumentResponse
from app.core.security import get_current_user

router = APIRouter(prefix="/v1/document", tags=["Document"])

@router.post("/upload", response_model=UploadDocumentResponse)
async def upload_document(
    file: UploadFile = File(..., description="The .xlsx file to parse"),
    user: dict = Depends(get_current_user),
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
        
        return {
            "documentId": doc_id,
            "questions": questions
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to process document: {e}")
