import uuid
from typing import Dict, Any, Optional

class DocumentStore:
    def __init__(self):
        # Maps document_id (str) -> { "sheets": dict, "questions": list }
        self._store: Dict[str, Any] = {}

    def save_document(self, data: Dict[str, Any], questions: list) -> str:
        doc_id = str(uuid.uuid4())
        self._store[doc_id] = {
            "data": data, # Raw parsed sheets
            "questions": questions # Flattened Question objects
        }
        return doc_id

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(doc_id)

    def update_question_answer(self, doc_id: str, question_id: str, answer: str):
        if doc_id in self._store:
            for q in self._store[doc_id]["questions"]:
                if q["id"] == question_id:
                    q["answer"] = answer
                    return True
        return False

# Global singleton
document_store = DocumentStore()
