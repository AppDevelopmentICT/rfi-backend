import asyncio
import logging
import uuid
from typing import Optional
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.db.database import Document, SessionLocal
from app.config import OLLAMA_API, OLLAMA_EMBEDDING_MODEL, DATABASE_URL, LANGCHAIN_DATABASE_URL
from app.services.external.docling import parse_document

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGVector

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
_INGEST_SEMAPHORE = asyncio.Semaphore(3)

_embeddings_model = None


def _get_embeddings_model() -> OllamaEmbeddings:
    global _embeddings_model
    if _embeddings_model is None:
        _embeddings_model = OllamaEmbeddings(model=OLLAMA_EMBEDDING_MODEL)
    return _embeddings_model


_vector_store = None


def _get_vector_store() -> PGVector:
    global _vector_store
    if _vector_store is None:
        _vector_store = PGVector(
            embeddings=_get_embeddings_model(),
            collection_name="knowledge_base",
            connection=LANGCHAIN_DATABASE_URL,
            use_jsonb=True,
            create_extension=True,
        )
    return _vector_store


def _add_chunks_sync(chunks, chunk_ids):
    vector_store = _get_vector_store()
    total = len(chunks)
    for i in range(0, total, BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        batch_ids = chunk_ids[i : i + BATCH_SIZE]
        logger.info(f"Pushing batch {i // BATCH_SIZE + 1} ({len(batch)} chunks) to PGVector")
        vector_store.add_documents(batch, ids=batch_ids)


def _update_doc_status(doc_id: int, status: str):
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            doc.status = status
            db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Could not update doc {doc_id} status to '{status}': {e}")
    finally:
        db.close()


async def _run_ingestion(
    file_bytes: bytes,
    filename: str,
    minio_key: Optional[str] = None,
    source: str = "upload",
    minio_etag: Optional[str] = None,
):
    logger.info(f"Starting Langchain ingestion pipeline for {filename}")

    short_db = SessionLocal()
    try:
        new_doc = Document(
            filename=filename,
            status="processing",
            minio_key=minio_key,
            minio_etag=minio_etag,
            source=source,
        )
        short_db.add(new_doc)
        short_db.commit()
        short_db.refresh(new_doc)
        doc_id = new_doc.id
    finally:
        short_db.close()

    try:
        parsed_text = await parse_document(file_bytes, filename)

        logger.info(f"Docling parsed {filename} into {len(parsed_text)} characters.")

        base_doc = LCDocument(
            page_content=parsed_text,
            metadata={"source": filename, "document_id": doc_id}
        )

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len
        )
        chunks = text_splitter.split_documents([base_doc])
        logger.info(f"Split {filename} into {len(chunks)} contextual chunks via Langchain.")

        if not chunks:
            logger.error("No chunks generated from document!")
            _update_doc_status(doc_id, "failed")
            return {"status": "error", "message": "No chunks generated"}

        chunk_ids = [str(uuid.uuid4()) for _ in range(len(chunks))]

        async with _INGEST_SEMAPHORE:
            await asyncio.to_thread(_add_chunks_sync, chunks, chunk_ids)

        _update_doc_status(doc_id, "completed")
        logger.info(f"Successfully processed and stored {filename}!")

        return {
            "status": "success",
            "document_id": doc_id,
            "chunks_processed": len(chunks),
            "docling_markdown": parsed_text,
        }

    except Exception as e:
        logger.error(f"Ingestion pipeline failed for {filename}: {e}")
        _update_doc_status(doc_id, "failed")
        raise e


async def process_document_pipeline(
    file: UploadFile,
    minio_key: Optional[str] = None,
    source: str = "upload",
    minio_etag: Optional[str] = None,
):
    file_bytes = await file.read()
    return await _run_ingestion(file_bytes, file.filename, minio_key, source, minio_etag)


async def process_document_pipeline_from_bytes(
    file_bytes: bytes,
    filename: str,
    minio_key: Optional[str] = None,
    source: str = "minio",
    minio_etag: Optional[str] = None,
):
    return await _run_ingestion(file_bytes, filename, minio_key, source, minio_etag)

