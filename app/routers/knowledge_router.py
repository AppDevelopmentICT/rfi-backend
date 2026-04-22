from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.services.knowledge.ingestion import process_document_pipeline
from app.services.knowledge.sync import sync_knowledge_base
from app.services.external.minio_client import delete_object as minio_delete_object
from app.db.database import get_db, Document
from app.core.security import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["Knowledge Base"])

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


@router.post("/sync")
async def sync_from_minio(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    """
    Sync knowledge base with MinIO bucket.
    Ingests new files, hard-deletes removed ones.
    """
    try:
        result = await sync_knowledge_base(db)
        return result
    except Exception as e:
        logger.error(f"MinIO sync failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.get("/documents")
async def list_documents(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    """List all documents in the knowledge base."""
    try:
        docs = db.query(Document).order_by(Document.id.desc()).all()
        return {
            "documents": [
                {
                    "id": doc.id,
                    "filename": doc.filename,
                    "status": doc.status,
                    "source": doc.source or "upload",
                    "minio_key": doc.minio_key,
                }
                for doc in docs
            ],
            "total": len(docs),
        }
    except Exception as e:
        logger.error(f"Failed to list documents: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    """Hard delete a document, its vector chunks, and its MinIO object."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        result = db.execute(
            text(
                "DELETE FROM langchain_pg_embedding "
                "WHERE cmetadata->>'document_id' = :doc_id"
            ),
            {"doc_id": str(document_id)},
        )
        deleted_chunks = result.rowcount

        if doc.minio_key:
            try:
                minio_delete_object(doc.minio_key)
            except Exception as minio_err:
                logger.warning(f"MinIO delete failed for '{doc.minio_key}': {minio_err}")

        db.delete(doc)
        db.commit()

        logger.info(f"Deleted document {document_id} and {deleted_chunks} vector chunks")
        return {
            "status": "success",
            "document_id": document_id,
            "chunks_deleted": deleted_chunks,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete document {document_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

