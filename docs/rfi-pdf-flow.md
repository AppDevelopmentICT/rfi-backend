# PDF-based RFI Flow

A separate end-to-end pipeline for RFI documents that arrive as PDF files. It
runs in parallel with the existing Excel-based RFI flow and never modifies
that path.

## High-level flow

1. **Upload** (`POST /api/v1/rfi-pdf/upload`)
   - Accepts a single `.pdf` file (max 25 MB).
   - Creates an `RFIPdfProject` row with status `uploading` and immediately
     returns the document payload so the UI can navigate to the editor.
   - Schedules `_run_pdf_pipeline` as a background task.

2. **Background pipeline** (`_run_pdf_pipeline`)
   - `STATUS_PARSING` — [Docling](https://docling-project.github.io/docling/) converts
     the PDF to Markdown (via **docling-serve** HTTP by default, or the Python `docling`
     package if `DOCLING_MODE` is configured — see backend `requirements-docling-embedded.txt`).
   - `STATUS_EXTRACTING` — Ollama (`extract_requirements`) returns structured
     requirements (project counts, engineer experience, etc.).
   - `STATUS_DRAFTING` — Ollama (`draft_response_markdown`) drafts a full
     Markdown response that the editor will display.
   - `STATUS_READY` — the document is ready for human editing.
   - Failures store the message in `error_message` and audit the failure.

3. **Editor (frontend `/rfi-pdf/[id]`)**
   - WYSIWYG-first TipTap editor with optional markdown source panel that
     stays in sync via `marked` and `turndown`.
   - Right sidebar feeds (`/master-data/projects` and `/master-data/engineers`)
     that support drag/drop and click-to-insert.
   - `POST /api/v1/rfi-pdf/{id}/regenerate` re-runs extraction + drafting.

4. **Preview & export**
   - `GET /api/v1/rfi-pdf/{id}/preview` renders the current draft on demand.
   - `POST /api/v1/rfi-pdf/{id}/export` returns the final PDF download.

## Status enum

| Status        | Meaning                                                 |
|---------------|---------------------------------------------------------|
| `uploading`   | Project row created, file still in flight.              |
| `parsing`     | Docling is converting the PDF.                          |
| `extracting`  | LLM is identifying requirements.                        |
| `drafting`    | LLM is writing the markdown draft.                      |
| `ready`       | Draft is available; editor can be opened.               |
| `failed`      | Pipeline failed; `error_message` contains details.      |

## Audit actions

All emitted with `resource_type="rfi_pdf_project"` and `rfi_pdf_project_id`.

- `rfi_pdf.uploaded`
- `rfi_pdf.parse_failed`
- `rfi_pdf.generated`
- `rfi_pdf.pipeline_failed`
- `rfi_pdf.lock_acquired`
- `rfi_pdf.lock_released`
- `rfi_pdf.save`
- `rfi_pdf.regenerate_started`
- `rfi_pdf.regenerated`
- `rfi_pdf.regenerate_failed`
- `rfi_pdf.export`
- `rfi_pdf.soft_delete`

## Database

- New table `rfi_pdf_projects` with the same lock semantics as `rfi_projects`.
- Audit log gains an optional `rfi_pdf_project_id` foreign key.
- Master data is read-only from existing `master_projects`,
  `master_project_products`, `master_user_profiles` tables populated by
  `backend/import_data.py`. When those tables are absent, the sidebar feeds
  return empty results so the UI degrades gracefully.

## Operational limits

- Upload max size: 25 MB.
- Markdown draft size: 200 000 characters (frontend hard cap, server accepts
  up to 400 000 with a stricter Pydantic limit).
- Docling timeout: configured via `DOCLING_DOCUMENT_TIMEOUT`.
- LLM timeouts: 180 s (extraction) and 240 s (draft).

## Local tests

```
python -m unittest discover -s tests
```

Covers `pdf_render`, `pdf_extraction` helpers, and the master-data degraded
mode.
