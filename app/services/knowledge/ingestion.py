import logging
import uuid
from typing import Optional
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.db.database import Document, engine
from app.config import OLLAMA_API, OLLAMA_EMBEDDING_MODEL, DATABASE_URL, LANGCHAIN_DATABASE_URL
from app.services.external.docling import parse_document

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGVector

logger = logging.getLogger(__name__)


async def _run_ingestion(
    file_bytes: bytes,
    filename: str,
    db: Session,
    minio_key: Optional[str] = None,
    source: str = "upload",
):
    """Core ingestion logic shared by both upload and sync paths."""
    logger.info(f"Starting Langchain ingestion pipeline for {filename}")
    
    new_doc = Document(
        filename=filename,
        status="processing",
        minio_key=minio_key,
        source=source,
    )
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)
    
    try:
        parsed_text = await parse_document(file_bytes, filename)
        
        logger.info(f"Docling parsed {filename} into {len(parsed_text)} characters.")
        
        base_doc = LCDocument(
            page_content=parsed_text, 
            metadata={"source": filename, "document_id": new_doc.id}
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
            return {"status": "error", "message": "No chunks generated"}
            
        chunk_texts = [c.page_content for c in chunks]
        logger.info(f"First chunk preview: {chunk_texts[0][:100] if chunk_texts else 'EMPTY'}")
        
        chunk_ids = [str(uuid.uuid4()) for _ in range(len(chunks))]
        
        embeddings_model = OllamaEmbeddings(
            model=OLLAMA_EMBEDDING_MODEL
        )
        
        try:
            logger.info(f"Testing embedding generation for 1st chunk...")
            test_embed = embeddings_model.embed_query(chunk_texts[0])
            logger.info(f"Successfully generated test embedding of length {len(test_embed)}")
        except Exception as e:
            logger.error(f"Embedding generation failed: {str(e)}")
            raise e

        vector_store = PGVector(
            embeddings=embeddings_model,
            collection_name="knowledge_base",
            connection=LANGCHAIN_DATABASE_URL,
            use_jsonb=True,
            create_extension=True
        )
        
        logger.info(f"Pushing {len(chunks)} chunks to PGVector...")
        vector_store.add_documents(chunks, ids=chunk_ids)
        
        new_doc.status = "completed"
        db.commit()
        logger.info(f"Successfully processed and stored {filename}!")
        
        return {"status": "success", "document_id": new_doc.id, "chunks_processed": len(chunks)}
        
    except Exception as e:
        logger.error(f"Ingestion pipeline failed for {filename}: {e}")
        new_doc.status = "failed"
        db.commit()
        raise e


async def process_document_pipeline(
    file: UploadFile,
    db: Session,
    minio_key: Optional[str] = None,
    source: str = "upload",
):
    """Ingest from an UploadFile (HTTP upload path)."""
    file_bytes = await file.read()
    return await _run_ingestion(file_bytes, file.filename, db, minio_key, source)


async def process_document_pipeline_from_bytes(
    file_bytes: bytes,
    filename: str,
    db: Session,
    minio_key: Optional[str] = None,
    source: str = "minio",
):
    """Ingest from raw bytes (MinIO sync path)."""
    return await _run_ingestion(file_bytes, filename, db, minio_key, source)

