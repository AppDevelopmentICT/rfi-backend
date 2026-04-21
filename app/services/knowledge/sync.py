import logging
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.database import Document
from app.services.external.minio_client import list_bucket_objects, download_object
from app.services.knowledge.ingestion import process_document_pipeline_from_bytes
from app.config import LANGCHAIN_DATABASE_URL

logger = logging.getLogger(__name__)

# Supported file extensions for knowledge base ingestion
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".doc"}


def _get_file_extension(filename: str) -> str:
    """Extract lowercase file extension from filename."""
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ""


def _is_supported_file(key: str) -> bool:
    """Check if file extension is supported for ingestion."""
    return _get_file_extension(key) in SUPPORTED_EXTENSIONS


def _delete_document_vectors(document_id: int, db: Session):
    """Hard delete all vector chunks associated with a document from PGVector."""
    try:
        # Delete from langchain's PGVector embedding table
        # The document_id is stored in cmetadata as a JSON field
        result = db.execute(
            text(
                "DELETE FROM langchain_pg_embedding "
                "WHERE cmetadata->>'document_id' = :doc_id"
            ),
            {"doc_id": str(document_id)},
        )
        deleted_count = result.rowcount
        logger.info(f"Deleted {deleted_count} vector chunks for document_id={document_id}")
        return deleted_count
    except Exception as e:
        logger.error(f"Failed to delete vectors for document_id={document_id}: {e}")
        raise


async def sync_knowledge_base(db: Session) -> Dict[str, Any]:
    """
    Full diff sync between MinIO bucket and knowledge base.
    
    1. List all objects in MinIO bucket
    2. Compare with existing minio-sourced documents in DB
    3. Ingest new files, hard-delete removed ones
    """
    logger.info("Starting MinIO knowledge base sync...")
    
    # 1. List MinIO objects (filter supported files only)
    minio_objects = list_bucket_objects()
    minio_keys = {
        obj["key"]: obj
        for obj in minio_objects
        if _is_supported_file(obj["key"])
    }
    
    logger.info(f"Found {len(minio_keys)} supported files in MinIO bucket")
    
    # 2. Get existing minio-sourced documents from DB
    existing_docs = db.query(Document).filter(Document.source == "minio").all()
    existing_keys = {doc.minio_key: doc for doc in existing_docs if doc.minio_key}
    
    logger.info(f"Found {len(existing_keys)} existing minio-sourced documents in DB")
    
    # 3. Determine diff
    new_keys = set(minio_keys.keys()) - set(existing_keys.keys())
    removed_keys = set(existing_keys.keys()) - set(minio_keys.keys())
    common_keys = set(minio_keys.keys()) & set(existing_keys.keys())

    updated_keys = set()
    unchanged_count = 0
    for key in common_keys:
        existing_doc = existing_keys[key]
        current_etag = minio_keys[key].get("etag", "")
        if existing_doc.minio_etag is None or existing_doc.minio_etag != current_etag:
            updated_keys.add(key)
        else:
            unchanged_count += 1

    added_files = []
    removed_files = []
    updated_files = []
    errors = []

    # 4. Ingest new files
    for key in new_keys:
        try:
            logger.info(f"Ingesting new file from MinIO: {key}")
            file_bytes = download_object(key)
            filename = key.rsplit("/", 1)[-1] if "/" in key else key
            etag = minio_keys[key].get("etag", "")

            result = await process_document_pipeline_from_bytes(
                file_bytes=file_bytes,
                filename=filename,
                db=db,
                minio_key=key,
                source="minio",
                minio_etag=etag,
            )
            added_files.append({
                "key": key,
                "filename": filename,
                "document_id": result["document_id"],
                "chunks_processed": result["chunks_processed"],
            })
        except Exception as e:
            logger.error(f"Failed to ingest '{key}': {e}")
            errors.append({"key": key, "error": str(e)})

    # 4b. Re-ingest updated files (delete old vectors first, then re-ingest)
    for key in updated_keys:
        try:
            existing_doc = existing_keys[key]
            logger.info(f"Re-ingesting updated file from MinIO: {key} (etag changed)")
            _delete_document_vectors(existing_doc.id, db)
            db.delete(existing_doc)
            db.commit()

            file_bytes = download_object(key)
            filename = key.rsplit("/", 1)[-1] if "/" in key else key
            etag = minio_keys[key].get("etag", "")

            result = await process_document_pipeline_from_bytes(
                file_bytes=file_bytes,
                filename=filename,
                db=db,
                minio_key=key,
                source="minio",
                minio_etag=etag,
            )
            updated_files.append({
                "key": key,
                "filename": filename,
                "document_id": result["document_id"],
                "chunks_processed": result["chunks_processed"],
            })
        except Exception as e:
            logger.error(f"Failed to re-ingest updated file '{key}': {e}")
            db.rollback()
            errors.append({"key": key, "error": str(e)})
    
    # 5. Hard delete removed files
    for key in removed_keys:
        try:
            doc = existing_keys[key]
            logger.info(f"Removing document '{key}' (id={doc.id}) — no longer in MinIO")
            
            # Delete vector chunks first
            _delete_document_vectors(doc.id, db)
            
            # Delete document record
            db.delete(doc)
            db.commit()
            
            removed_files.append({"key": key, "filename": doc.filename, "document_id": doc.id})
        except Exception as e:
            logger.error(f"Failed to remove document '{key}': {e}")
            db.rollback()
            errors.append({"key": key, "error": str(e)})
    
    summary = {
        "status": "success",
        "added": added_files,
        "updated": updated_files,
        "removed": removed_files,
        "unchanged": unchanged_count,
        "errors": errors,
        "total_in_bucket": len(minio_keys),
        "total_in_db": len(existing_keys) + len(added_files) + len(updated_files) - len(removed_files),
    }
    
    logger.info(
        f"Sync complete: +{len(added_files)} added, "
        f"~{len(updated_files)} updated, "
        f"-{len(removed_files)} removed, "
        f"={unchanged_count} unchanged, "
        f"!{len(errors)} errors"
    )
    
    return summary
