from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from sqlalchemy.orm import Session
from app.services.knowledge.ingestion import process_document_pipeline
from app.db.database import get_db
from app.core.security import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/knowledge", tags=["Knowledge Base"])

@router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(..., description="A file to upload into the Knowledge Base (PDF, DOCX, TXT)"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    """
    Ingests a document into the Knowledge base.
    Uses Docling for extraction, Langchain for split, and PGVector/Ollama for embeddings.
    """
    
    if not file.filename:
        raise HTTPException(
            status_code=422,
            detail="File has no filename associated."
        )

    try:

        result = await process_document_pipeline(file, db)
        return result
    except Exception as e:
        logger.error(f"Failed to process and ingest document {file.filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
