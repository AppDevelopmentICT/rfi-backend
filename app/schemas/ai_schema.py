from typing import Any, Optional
from pydantic import BaseModel, Field

class Question(BaseModel):
    id: str
    number: int
    question: str
    answer: str
    originalAnswer: str
    status: str = "idle"

class UploadDocumentResponse(BaseModel):
    documentId: str
    questions: list[Question]

class GenerateAllRequest(BaseModel):
    documentId: str
    questions: list[Question]

class GenerateResult(BaseModel):
    id: str
    answer: str
    sources: list[str] = []

class GenerateAllResponse(BaseModel):
    results: list[GenerateResult]

class RegenerateRequest(BaseModel):
    documentId: str
    questionId: str
    prompt: Optional[str] = None

class RegenerateResponse(BaseModel):
    id: str
    answer: str
    sources: list[str] = []

class SaveQuestionsRequest(BaseModel):
    questions: list[Question]

class SaveQuestionsResponse(BaseModel):
    documentId: str
