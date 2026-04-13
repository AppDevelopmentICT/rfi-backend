from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
import io
from typing import Optional

from app.config import OLLAMA_MODEL
from app.services.rfi_service import parse_excel_bytes, auto_fill_bytes
from app.schemas.excel_schema import AutoFillResponse, ErrorResponse

# ─── Error responses for OpenAPI docs ────────────────────────────────
ERROR_RESPONSES = {
    404: {
        "model": ErrorResponse,
        "description": "No valid Excel file was provided",
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

router = APIRouter(prefix="/api/rfi", tags=["RFI/RFP"])


# ─── POST /api/rfi/read — upload Excel and get parsed data ──────────
@router.post(
    "/read",
    summary="Read Uploaded Excel",
    description=(
        "Upload an `.xlsx` file and receive all sheets parsed as JSON.\n\n"
        "**No file is stored on the server** — processing is fully in-memory."
    ),
    responses={404: ERROR_RESPONSES[404]},
)
async def read_uploaded_excel(
    file: UploadFile = File(..., description="The .xlsx file to parse"),
):
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=422,
            detail="Only .xlsx or .xls files are accepted",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Uploaded file is empty")

    try:
        return parse_excel_bytes(file_bytes)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to parse Excel: {e}")


# ─── POST /api/rfi/auto-fill — upload Excel, fill with LLM, download ─
@router.post(
    "/auto-fill",
    summary="Auto-Fill Uploaded Excel with LLM",
    description=(
        "Upload an `.xlsx` file and let the LLM fill every empty cell.\n\n"
        "**Payload:**\n"
        "- `file` (required) — the `.xlsx` file\n"
        "- `model` (optional) — Ollama model name (default: `mistral:7b`)\n\n"
        "**Returns** the filled `.xlsx` file as a download."
    ),
    responses={
        200: {
            "content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
            "description": "The filled Excel file as a download",
        },
        404: ERROR_RESPONSES[404],
        422: ERROR_RESPONSES[422],
        502: ERROR_RESPONSES[502],
    },
)
async def auto_fill_uploaded_excel(
    file: UploadFile = File(..., description="The .xlsx file to auto-fill"),
    model: Optional[str] = Form(
        default=None,
        description=f"Ollama model name (default: {OLLAMA_MODEL})",
    ),
):
    # ── validate file ───────────────────────────────────────────────
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=422,
            detail="Only .xlsx or .xls files are accepted",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Uploaded file is empty")

    # ── run auto-fill ───────────────────────────────────────────────
    try:
        result = await auto_fill_bytes(file_bytes, model=model)
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Ollama service unreachable: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected error: {e}")

    if not result["results"]:
        raise HTTPException(
            status_code=422,
            detail="No fillable cells found — check that sheets have a 'Question' column with empty answer cells",
        )

    # ── return filled file as download ──────────────────────────────
    output_filename = file.filename.rsplit(".", 1)[0] + "_answered.xlsx"

    return StreamingResponse(
        io.BytesIO(result["filled_bytes"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{output_filename}"',
            "X-AutoFill-Message": result["message"],
        },
    )
