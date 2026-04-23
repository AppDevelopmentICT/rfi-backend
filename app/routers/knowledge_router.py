from fastapi import APIRouter, File, HTTPException, UploadFile, Depends, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.services.knowledge.ingestion import process_document_pipeline
from app.services.knowledge.sync import sync_knowledge_base
from app.services.external.minio_client import delete_object as minio_delete_object
from app.db.database import get_db, Document
from app.core.security import get_current_user
from app.schemas.knowledge_schema import SortField, SortDirection
from typing import List
from pydantic import BaseModel
import logging
import math

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["Knowledge Base"])

@router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(..., description="A file to upload into the Knowledge Base (PDF, DOCX, TXT)"),
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
        result = await process_document_pipeline(file)
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
def list_documents(
    search: str = Query("", description="Search query for filename, source, or status"),
    sort_by: SortField = Query(SortField.created_at, description="Field to sort by"),
    sort_dir: SortDirection = Query(SortDirection.desc, description="Sort direction"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    """List documents with search, sort, and pagination."""
    try:
        query = db.query(Document)

        if search.strip():
            pattern = f"%{search.strip()}%"
            query = query.filter(
                Document.filename.ilike(pattern)
                | Document.source.ilike(pattern)
                | Document.status.ilike(pattern)
            )

        total = query.count()

        sort_column = getattr(Document, sort_by.value, Document.created_at)
        if sort_dir == SortDirection.desc:
            sort_column = sort_column.desc()
        else:
            sort_column = sort_column.asc()

        docs = (
            query.order_by(sort_column)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        total_pages = math.ceil(total / per_page) if total > 0 else 1

        return {
            "documents": [
                {
                    "id": doc.id,
                    "filename": doc.filename,
                    "status": doc.status,
                    "source": doc.source or "upload",
                    "minio_key": doc.minio_key,
                    "created_at": doc.created_at.isoformat() if doc.created_at else None,
                }
                for doc in docs
            ],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
    except Exception as e:
        logger.error(f"Failed to list documents: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/documents/{document_id}")
def delete_document(
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


class BulkDeleteRequest(BaseModel):
    document_ids: List[int]


@router.post("/documents/bulk-delete")
def bulk_delete_documents(
    req: BulkDeleteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Bulk delete documents, their vector chunks, and MinIO objects."""
    deleted = 0
    failed = []

    for doc_id in req.document_ids:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            failed.append({"id": doc_id, "error": "not found"})
            continue

        try:
            db.execute(
                text(
                    "DELETE FROM langchain_pg_embedding "
                    "WHERE cmetadata->>'document_id' = :doc_id"
                ),
                {"doc_id": str(doc_id)},
            )

            if doc.minio_key:
                try:
                    minio_delete_object(doc.minio_key)
                except Exception as minio_err:
                    logger.warning(f"MinIO delete failed for '{doc.minio_key}': {minio_err}")

            db.delete(doc)
            deleted += 1
        except Exception as e:
            failed.append({"id": doc_id, "error": str(e)})
            logger.error(f"Failed to delete document {doc_id}: {e}")

    if deleted > 0:
        db.commit()
    else:
        db.rollback()

    logger.info(f"Bulk deleted {deleted} documents, {len(failed)} failed")
    return {"deleted": deleted, "failed": failed}

