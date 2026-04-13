import io
from typing import Optional

from openpyxl import load_workbook
from openpyxl.styles import Alignment

from app.services.ollama_service import ask_ollama


def parse_excel_bytes(file_bytes: bytes) -> dict:
    """Parse uploaded Excel bytes and return all sheets with headers + data."""
    wb = load_workbook(io.BytesIO(file_bytes))
    sheets = {}

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows:
            sheets[sheet_name] = {"headers": [], "data": []}
            continue

        headers = [
            str(h) if h is not None else f"col_{i}"
            for i, h in enumerate(all_rows[0])
        ]
        data = []
        for row in all_rows[1:]:
            row_dict = {}
            for idx, val in enumerate(row):
                key = headers[idx] if idx < len(headers) else f"col_{idx}"
                row_dict[key] = val
            data.append(row_dict)

        sheets[sheet_name] = {"headers": headers, "data": data}

    wb.close()
    return sheets


async def auto_fill_bytes(
    file_bytes: bytes,
    model: Optional[str] = None,
) -> dict:
    """Fill empty cells in an uploaded Excel file using the LLM.

    Args:
        file_bytes: Raw bytes of the uploaded .xlsx file.
        model:      Optional Ollama model name; falls back to config default.

    Returns:
        Dict with ``filled_bytes`` (bytes of the output workbook),
        ``message``, and ``results`` list.
    """
    wb = load_workbook(io.BytesIO(file_bytes))
    results = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        all_rows = list(ws.iter_rows(values_only=True))
        if len(all_rows) < 2:
            continue

        headers = [
            str(h).strip() if h is not None else "" for h in all_rows[0]
        ]

        # find the question column
        question_col = None
        for i, h in enumerate(headers):
            if "question" in h.lower():
                question_col = i
                break

        if question_col is None:
            continue

        # find every column that needs filling
        fill_cols = []
        for i, h in enumerate(headers):
            if i == question_col:
                continue
            low = h.lower()
            if low in ("no", "no.", "#", "number", ""):
                continue
            fill_cols.append(i)

        if not fill_cols:
            continue

        # iterate rows and fill empty cells
        for row_idx, row in enumerate(all_rows[1:], start=2):
            question = row[question_col]
            if not question or str(question).strip() == "":
                continue

            for col_idx in fill_cols:
                existing = row[col_idx] if col_idx < len(row) else None
                if existing is not None and str(existing).strip() != "":
                    continue  # already answered, skip

                col_header = headers[col_idx]
                answer = await ask_ollama(
                    str(question).strip(), col_header, model=model
                )

                cell = ws.cell(
                    row=row_idx, column=col_idx + 1, value=answer
                )
                cell.alignment = Alignment(wrap_text=True, vertical="top")

                results.append(
                    {
                        "sheet": sheet_name,
                        "row": row_idx,
                        "column": col_header,
                        "question": str(question).strip(),
                        "answer": answer,
                    }
                )

    # save to in-memory buffer
    output_buf = io.BytesIO()
    wb.save(output_buf)
    wb.close()
    output_buf.seek(0)

    return {
        "filled_bytes": output_buf.getvalue(),
        "message": f"Filled {len(results)} cells",
        "results": results,
    }
