from typing import Optional, List
from pydantic import BaseModel, Field
from enum import Enum


class SortField(str, Enum):
    filename = "filename"
    status = "status"
    source = "source"
    created_at = "created_at"


class SortDirection(str, Enum):
    asc = "asc"
    desc = "desc"


class KBDocumentOut(BaseModel):
    id: int
    filename: str
    status: str
    source: str
    minio_key: Optional[str] = None
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}


class PaginatedDocumentsResponse(BaseModel):
    documents: List[KBDocumentOut]
    total: int
    page: int
    per_page: int
    total_pages: int
