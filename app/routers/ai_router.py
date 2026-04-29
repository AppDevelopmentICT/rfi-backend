import logging

from fastapi import APIRouter, HTTPException, Depends, Request
from sqlalchemy.orm import Session

from app.core.security import get_current_user, CurrentUser
from app.db.database import get_db
from app.services.audit_service import log_audit

from app.services.external.ollama import ask_ollama, _retrieve_knowledge_context
from app.services.knowledge.storage import document_store
from app.schemas.ai_schema import (
    GenerateAllRequest,
    GenerateAllResponse,
    RegenerateRequest,
    RegenerateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["AI"])


def _caller_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _as_str_sources(sources: object) -> list[str]:
    """Ensure response fits GenerateAllResponse (list[str]); metadata may return non-str."""
    if not sources:
        return []
    try:
        return [str(s) if s is not None else "" for s in sources]  # type: ignore[arg-type]
    except TypeError:
        return []


@router.post("/generate-all", response_model=GenerateAllResponse)
async def generate_all(
    request: Request,
    req: GenerateAllRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if document_store.get_document(req.documentId) is None:
        raise HTTPException(
            status_code=404,
            detail="Document session expired or not found. Re-upload your Excel file and try again.",
        )

    results = []
    for q in req.questions:
        try:
            knowledge_context, sources = _retrieve_knowledge_context(q.question)

            if knowledge_context:
                logger.info(f"Retrieved knowledge context for question: {q.question[:80]}...")
            else:
                logger.info(f"No knowledge context found for: {q.question[:80]}... (using general knowledge)")

            yes_no_answer = await ask_ollama(
                q.question,
                "Yes/No",
                knowledge_context=knowledge_context,
            )

            why_answer = await ask_ollama(
                q.question,
                "Why?",
                knowledge_context=knowledge_context,
            )

            combined_answer = f"{yes_no_answer} — {why_answer}" if why_answer else yes_no_answer

            document_store.update_question_answer(req.documentId, q.id, combined_answer)

            results.append(
                {
                    "id": str(q.id),
                    "answer": combined_answer,
                    "sources": _as_str_sources(sources),
                }
            )
        except Exception as e:
            logger.error(f"Failed to generate answer for question {q.id}: {e}")
            results.append(
                {
                    "id": str(q.id),
                    "answer": f"Error: {e}",
                    "sources": [],
                }
            )

    log_audit(
        db,
        user_id=user.id if not user.is_service_account else None,
        action="ai.generate_all",
        resource_type="session",
        details={"document_id": req.documentId, "question_count": len(req.questions)},
        ip_address=_caller_ip(request),
    )

    return {"results": results}


@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate(
    request: Request,
    req: RegenerateRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    doc = document_store.get_document(req.documentId)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    question_text = ""
    for q in doc["questions"]:
        if q["id"] == req.questionId:
            question_text = q["question"]
            break

    if not question_text:
        if hasattr(req, "prompt") and req.prompt:
            question_text = req.prompt
        else:
            raise HTTPException(status_code=404, detail="Question context not found")

    try:
        knowledge_context, sources = _retrieve_knowledge_context(question_text)

        if knowledge_context:
            logger.info(f"Retrieved knowledge context for regeneration: {question_text[:80]}...")

        yes_no_answer = await ask_ollama(
            question_text,
            "Yes/No",
            knowledge_context=knowledge_context,
        )

        why_answer = await ask_ollama(
            question_text,
            "Why?",
            knowledge_context=knowledge_context,
        )

        combined_answer = f"{yes_no_answer} — {why_answer}" if why_answer else yes_no_answer

        document_store.update_question_answer(req.documentId, req.questionId, combined_answer)

        log_audit(
            db,
            user_id=user.id if not user.is_service_account else None,
            action="ai.regenerate",
            resource_type="session",
            details={"document_id": req.documentId, "question_id": req.questionId},
            ip_address=_caller_ip(request),
        )

        return {
            "id": req.questionId,
            "answer": combined_answer,
            "sources": _as_str_sources(sources),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")
