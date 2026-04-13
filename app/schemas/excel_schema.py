from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field

from app.config import OLLAMA_MODEL


# ─── Shared Error Schema ────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """Standard error envelope returned by every error response."""
    detail: str = Field(..., description="Human-readable error message")

    class Config:
        json_schema_extra = {
            "example": {"detail": "Something went wrong"}
        }


# ─── Sheet Data (used by both example and real APIs) ────────────────

class SheetData(BaseModel):
    """One sheet's content: headers list + rows as dicts."""
    headers: list[str] = Field(
        ...,
        description="Column header names from the first row",
        json_schema_extra={"example": ["No", "Question", "Answer", "Remark"]},
    )
    data: list[dict[str, Any]] = Field(
        ...,
        description="List of row objects keyed by header name",
        json_schema_extra={
            "example": [
                {"No": 1, "Question": "Does it support SSO?", "Answer": None, "Remark": None}
            ]
        },
    )


class ExcelReadResponse(BaseModel):
    """Response for read endpoints — keyed by sheet name."""

    class Config:
        json_schema_extra = {
            "example": {
                "Sheet1": {
                    "headers": ["No", "Question", "Answer"],
                    "data": [
                        {"No": 1, "Question": "Does it support SSO?", "Answer": None}
                    ],
                }
            }
        }


# ─── Filled Cell ────────────────────────────────────────────────────

class FilledCell(BaseModel):
    """One cell that was filled by the LLM."""
    sheet: str = Field(..., description="Sheet name")
    row: int = Field(..., description="1-indexed row number")
    column: str = Field(..., description="Column header name that was filled")
    context: str = Field(..., description="The context data from filled columns used as LLM input")
    answer: str = Field(..., description="LLM-generated answer")

    class Config:
        json_schema_extra = {
            "example": {
                "sheet": "Sheet1",
                "row": 2,
                "column": "Answer",
                "context": "Question: Does it support SSO?\nCategory: Security",
                "answer": "Mendix supports SSO via SAML 2.0 and OpenID Connect.",
            }
        }


# ─── Auto-Fill Response (example API, returns JSON) ─────────────────

class AutoFillResponse(BaseModel):
    """Response for the example POST /api/example/auto-fill (JSON body)."""
    message: str = Field(
        ...,
        description="Summary of how many cells were filled",
        json_schema_extra={"example": "Filled 12 cells"},
    )
    output_file: str = Field(
        ...,
        description="Filename of the saved Excel output",
        json_schema_extra={"example": "RFI-Examples_answered.xlsx"},
    )
    results: list[FilledCell] = Field(
        ..., description="Details of every cell that was filled"
    )


# ─── Auto-Fill Request Payload (real API) ────────────────────────────

class AutoFillRequest(BaseModel):
    """
    Multipart form fields for POST /api/rfi/auto-fill.

    | Field             | Type           | Required | Description                                                        |
    |-------------------|----------------|----------|--------------------------------------------------------------------|
    | `file`            | UploadFile     | ✅       | The `.xlsx` file to process                                        |
    | `model`           | string / null  | ❌       | Ollama model name (default: mistral:7b)                            |
    | `context_columns` | string / null  | ❌       | Comma-separated column names for LLM context (auto-detected if omitted) |
    | `fill_columns`    | string / null  | ❌       | Comma-separated column names for LLM to fill (auto-detected if omitted) |
    """
    model: Optional[str] = Field(
        default=None,
        description=f"Ollama model name override (default: {OLLAMA_MODEL})",
        json_schema_extra={"example": "mistral:7b"},
    )
    context_columns: Optional[str] = Field(
        default=None,
        description="Comma-separated column names to use as LLM context (e.g. 'Question,Category')",
        json_schema_extra={"example": "Question,Category"},
    )
    fill_columns: Optional[str] = Field(
        default=None,
        description="Comma-separated column names for the LLM to fill (e.g. 'Answer,Remark')",
        json_schema_extra={"example": "Answer,Remark"},
    )

