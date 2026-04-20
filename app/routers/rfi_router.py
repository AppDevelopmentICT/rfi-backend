from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
import io
from typing import Optional

from app.config import OLLAMA_MODEL
from app.services.rfi.core import parse_excel_bytes, auto_fill_bytes
from app.schemas.excel_schema import ErrorResponse
from app.schemas.ai_schema import SaveQuestionsRequest, SaveQuestionsResponse
from app.services.knowledge.storage import document_store


ERROR_RESPONSES = {
    404: {
        "model": ErrorResponse,
        "description": "No valid Excel file was provided",
    },
    422: {
        "model": ErrorResponse,
        "description": "Excel file has no fillable data or invalid columns specified",
    },
    502: {
        "model": ErrorResponse,
        "description": "Ollama LLM service is unreachable or returned an error",
    },
}

router = APIRouter(prefix="/api/v1/rfi", tags=["RFI/RFP"])



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


@router.post(
    "/save",
    summary="Save parsed questions",
    description="Save questions from column picker to document store. Returns a documentId for subsequent AI calls.",
    response_model=SaveQuestionsResponse,
)
async def save_questions(req: SaveQuestionsRequest):
    doc_id = document_store.save_document(
        data={},
        questions=[q.model_dump() for q in req.questions],
    )
    return SaveQuestionsResponse(documentId=doc_id)



@router.post(
    "/auto-fill",
    summary="Auto-Fill Uploaded Excel with LLM",
    description=(
        "Upload an `.xlsx` file and let the LLM fill every empty cell.\n\n"
        "**Column detection is fully dynamic:**\n"
        "- Context columns (input to LLM) = columns where >50% of rows have data\n"
        "- Fill columns (LLM output) = columns that have empty cells\n\n"
        "You can optionally override with `context_columns` and `fill_columns`.\n\n"
        "**Payload (multipart form):**\n"
        "| Field | Type | Required | Description |\n"
        "|---|---|---|---|\n"
        "| `file` | UploadFile | ✅ | The `.xlsx` file |\n"
        "| `model` | string | ❌ | Ollama model name (default: `mistral:7b`) |\n"
        "| `context_columns` | string | ❌ | Comma-separated column names to use as LLM context |\n"
        "| `fill_columns` | string | ❌ | Comma-separated column names for LLM to fill |\n\n"
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
    context_columns: Optional[str] = Form(
        default=None,
        description="Comma-separated column names to use as context/input for the LLM (e.g. 'Question,Category'). If omitted, auto-detected.",
    ),
    fill_columns: Optional[str] = Form(
        default=None,
        description="Comma-separated column names for the LLM to fill (e.g. 'Answer,Remark'). If omitted, auto-detected.",
    ),
):

    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=422,
            detail="Only .xlsx or .xls files are accepted",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Uploaded file is empty")


    ctx_cols = (
        [c.strip() for c in context_columns.split(",") if c.strip()]
        if context_columns
        else None
    )
    fill_cols = (
        [c.strip() for c in fill_columns.split(",") if c.strip()]
        if fill_columns
        else None
    )


    try:
        result = await auto_fill_bytes(
            file_bytes,
            model=model,
            context_columns=ctx_cols,
            fill_columns=fill_cols,
        )
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Ollama service unreachable: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected error: {e}")

    if not result["results"]:
        raise HTTPException(
            status_code=422,
            detail="No empty cells found to fill. Either all cells already have data, or no context/fill columns could be detected. Try specifying context_columns and fill_columns explicitly.",
        )


    output_filename = file.filename.rsplit(".", 1)[0] + "_answered.xlsx"

    return StreamingResponse(
        io.BytesIO(result["filled_bytes"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{output_filename}"',
            "X-AutoFill-Message": result["message"],
        },
    )

