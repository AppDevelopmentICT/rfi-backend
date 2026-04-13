from fastapi import APIRouter
from app.services.excel_service import read_all_sheets, auto_fill_sheets

router = APIRouter(prefix="/api", tags=["Excel"])


@router.get("/excel")
def get_excel():
    """Return all sheets, all rows, all columns from the Excel file."""
    return read_all_sheets()


@router.post("/auto-fill")
async def post_auto_fill():
    """Use LLM to fill every empty cell in every sheet."""
    return await auto_fill_sheets()
