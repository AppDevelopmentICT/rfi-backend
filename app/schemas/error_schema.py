from typing import Optional, Any
from pydantic import BaseModel, Field

class ErrorDetail(BaseModel):
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[Any] = Field(None, description="Optional extra error details")

class ErrorResponse(BaseModel):
    error: ErrorDetail
