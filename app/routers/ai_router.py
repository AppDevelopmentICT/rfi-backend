from fastapi import APIRouter, HTTPException, Depends
from app.core.security import get_current_user
from app.services.external.ollama import ask_ollama
from app.services.knowledge.storage import document_store
from app.schemas.ai_schema import (
    GenerateAllRequest, 
    GenerateAllResponse, 
    RegenerateRequest, 
    RegenerateResponse
)

router = APIRouter(prefix="/v1/ai", tags=["AI"])

@router.post("/generate-all", response_model=GenerateAllResponse)
async def generate_all(req: GenerateAllRequest, user: dict = Depends(get_current_user)):
    doc = document_store.get_document(req.documentId)
    if not doc:
        pass

    results = []
    for q in req.questions:
        try:
            answer = await ask_ollama(q.question, "Answer")
            
            document_store.update_question_answer(req.documentId, q.id, answer)
            
            results.append({"id": q.id, "answer": answer})
        except Exception as e:
            results.append({"id": q.id, "answer": f"Error: {e}"})
            
    return {"results": results}

@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate(req: RegenerateRequest, user: dict = Depends(get_current_user)):
    doc = document_store.get_document(req.documentId)
    question_text = ""
    
    if doc:
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
        answer = await ask_ollama(question_text, "Answer")
        document_store.update_question_answer(req.documentId, req.questionId, answer)
        return {"id": req.questionId, "answer": answer}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")
