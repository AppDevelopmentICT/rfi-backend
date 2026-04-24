import logging
from fastapi import APIRouter, HTTPException, Depends
from app.core.security import get_current_user
from app.services.external.ollama import ask_ollama, _retrieve_knowledge_context
from app.services.knowledge.storage import document_store
from app.schemas.ai_schema import (
    GenerateAllRequest, 
    GenerateAllResponse, 
    GenerateResult,
    RegenerateRequest, 
    RegenerateResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["AI"])

@router.post("/generate-all", response_model=GenerateAllResponse)
async def generate_all(req: GenerateAllRequest, user: dict = Depends(get_current_user)):
    document_store.get_document(req.documentId)

    results = []
    for q in req.questions:
        try:
            # Retrieve knowledge base context once per question
            knowledge_context, sources = _retrieve_knowledge_context(q.question)

            if knowledge_context:
                logger.info(f"Retrieved knowledge context for question: {q.question[:80]}...")
            else:
                logger.info(f"No knowledge context found for: {q.question[:80]}... (using general knowledge)")

            # Generate Yes/No answer
            yes_no_answer = await ask_ollama(
                q.question,
                "Yes/No",
                knowledge_context=knowledge_context,
            )

            # Generate explanation/reason
            why_answer = await ask_ollama(
                q.question,
                "Why?",
                knowledge_context=knowledge_context,
            )

            # Combine into a structured answer
            combined_answer = f"{yes_no_answer} — {why_answer}" if why_answer else yes_no_answer

            document_store.update_question_answer(req.documentId, q.id, combined_answer)
            
            results.append({"id": q.id, "answer": combined_answer, "sources": sources})
        except Exception as e:
            logger.error(f"Failed to generate answer for question {q.id}: {e}")
            results.append({"id": q.id, "answer": f"Error: {e}"})
            
    return {"results": results}

@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate(req: RegenerateRequest, user: dict = Depends(get_current_user)):
    doc = document_store.get_document(req.documentId)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    question_text = ""
    for q in doc["questions"]:
            if q["id"] == req.questionId:
                question_text = q["question"]
                break
    
    if not question_text:
        if hasattr(req, 'prompt') and req.prompt:
            question_text = req.prompt
        else:
            raise HTTPException(status_code=404, detail="Question context not found")

    try:
        # Retrieve knowledge base context
        knowledge_context, sources = _retrieve_knowledge_context(question_text)

        if knowledge_context:
            logger.info(f"Retrieved knowledge context for regeneration: {question_text[:80]}...")

        # Generate Yes/No answer
        yes_no_answer = await ask_ollama(
            question_text,
            "Yes/No",
            knowledge_context=knowledge_context,
        )

        # Generate explanation/reason
        why_answer = await ask_ollama(
            question_text,
            "Why?",
            knowledge_context=knowledge_context,
        )

        combined_answer = f"{yes_no_answer} — {why_answer}" if why_answer else yes_no_answer

        document_store.update_question_answer(req.documentId, req.questionId, combined_answer)
        return {"id": req.questionId, "answer": combined_answer, "sources": sources}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")
