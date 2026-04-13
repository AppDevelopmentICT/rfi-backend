from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


# ─── Shared Error Schema ────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """Standard error envelope returned by every error response."""
    detail: str = Field(..., description="Human-readable error message")

    class Config:
        json_schema_extra = {
            "example": {"detail": "Something went wrong"}
        }


# ─── GET /api/excel ─────────────────────────────────────────────────

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
    """Response for GET /api/excel — keyed by sheet name."""

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

    # FastAPI will serialise the actual dict[str, SheetData] return value;
    # the response_model is declared on the route for docs purposes.


# ─── POST /api/auto-fill ────────────────────────────────────────────

class FilledCell(BaseModel):
    """One cell that was filled by the LLM."""
    sheet: str = Field(..., description="Sheet name")
    row: int = Field(..., description="1-indexed row number")
    column: str = Field(..., description="Column header name")
    question: str = Field(..., description="The question text from that row")
    answer: str = Field(..., description="LLM-generated answer")

    class Config:
        json_schema_extra = {
            "example": {
                "sheet": "Sheet1",
                "row": 2,
                "column": "Answer",
                "question": "Does it support SSO?",
                "answer": "Mendix supports SSO via SAML 2.0 and OpenID Connect.",
            }
        }


class AutoFillResponse(BaseModel):
    """Response for POST /api/auto-fill."""
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
