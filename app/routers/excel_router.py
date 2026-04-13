from fastapi import APIRouter, HTTPException
from app.services.excel_service import read_all_sheets, auto_fill_sheets
from app.schemas.excel_schema import (
    AutoFillResponse,
    ErrorResponse,
)

# ─── Shared error responses shown in OpenAPI docs ────────────────────
ERROR_RESPONSES = {
    404: {
        "model": ErrorResponse,
        "description": "Excel source file not found on the server",
    },
    422: {
        "model": ErrorResponse,
        "description": "Excel file has no fillable data (missing question column or empty sheets)",
    },
    502: {
        "model": ErrorResponse,
        "description": "Ollama LLM service is unreachable or returned an error",
    },
}

router = APIRouter(prefix="/api", tags=["Excel"])


@router.get(
    "/excel",
    summary="Read Excel File",
    description="Return all sheets, all rows, all columns from the RFI Excel file.",
    responses={
        404: ERROR_RESPONSES[404],
    },
)
def get_excel():
    try:
        return read_all_sheets()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Excel source file not found")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected error: {e}")


@router.post(
    "/auto-fill",
    response_model=AutoFillResponse,
    summary="Auto-Fill with LLM",
    description="Use the Ollama LLM to fill every empty cell in every sheet of the Excel file.",
    responses={
        404: ERROR_RESPONSES[404],
        422: ERROR_RESPONSES[422],
        502: ERROR_RESPONSES[502],
    },
)
async def post_auto_fill():
    try:
        result = await auto_fill_sheets()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Excel source file not found")
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Ollama service unreachable: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected error: {e}")

    if not result["results"]:
        raise HTTPException(
            status_code=422,
            detail="No fillable cells found — check that sheets have a 'Question' column with empty answer cells",
        )

    return result
