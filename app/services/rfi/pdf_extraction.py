"""LLM-driven requirement extraction and draft generation for PDF-based RFI documents.

The pipeline does three things sequentially:
1. Run Docling to convert the PDF bytes into a markdown representation.
2. Ask the LLM to extract structured requirements (project counts, engineer experience, etc.).
3. Ask the LLM to draft a markdown response document grounded in the parsed source.

All LLM responses are normalized so that the editor receives clean, valid markdown.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import OLLAMA_API, OLLAMA_MODEL

logger = logging.getLogger(__name__)

_MAX_MARKDOWN_CHARS = 24000
_EXTRACTION_TIMEOUT = 180.0
_DRAFT_TIMEOUT = 240.0


def _truncate_for_prompt(markdown: str, max_chars: int = _MAX_MARKDOWN_CHARS) -> str:
    """Limit markdown context fed to the LLM so we stay within model context.

    Keeps the leading section (which usually contains the request scope) and
    appends a short tail with the closing sections so we don't lose the
    request signature / contact info.
    """
    if not markdown:
        return ""
    if len(markdown) <= max_chars:
        return markdown
    head_size = int(max_chars * 0.75)
    tail_size = max(0, max_chars - head_size - 32)
    head = markdown[:head_size]
    tail = markdown[-tail_size:] if tail_size else ""
    return f"{head}\n\n... (truncated for processing) ...\n\n{tail}"


def _strip_code_fences(text: str) -> str:
    fence = re.match(r"^\s*```(?:json|markdown|md)?\s*\n(.+?)\n```\s*$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text.strip()


def _safe_json_loads(text: str) -> Any:
    cleaned = _strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


async def _generate(prompt: str, system: str, *, model: str | None, timeout: float, num_predict: int) -> str:
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": num_predict},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{OLLAMA_API}/api/generate", json=payload)
        response.raise_for_status()
        data = response.json()
        return str(data.get("response") or "").strip()


_EXTRACTION_SYSTEM = (
    "You are an analyst that reads RFI (Request for Information) source documents.\n"
    "Identify every requirement, question, or data ask in the text. Output STRICT JSON only.\n"
    "Schema:\n"
    "{\n"
    "  \"title\": string,\n"
    "  \"summary\": string,\n"
    "  \"requirements\": [\n"
    "    {\n"
    "      \"id\": string (stable slug, e.g. req-1),\n"
    "      \"category\": one of \"projects\" | \"engineers\" | \"company\" | \"general\",\n"
    "      \"prompt\": string,\n"
    "      \"projects\": [string],\n"
    "      \"products\": [string],\n"
    "      \"min_experience_years\": number | null,\n"
    "      \"timeframe_years\": number | null\n"
    "    }\n"
    "  ],\n"
    "  \"language\": string (\"en\" or \"id\")\n"
    "}\n"
    "Rules:\n"
    "- Return ONLY valid JSON, no commentary, no markdown fences.\n"
    "- Categorize numeric / project history questions as \"projects\".\n"
    "- Categorize engineer / personnel questions as \"engineers\".\n"
    "- Categorize org/company background questions as \"company\".\n"
    "- Keep `prompt` faithful to the source wording.\n"
)


async def extract_requirements(parsed_markdown: str, *, model: str | None = None) -> dict[str, Any]:
    """Use the LLM to extract structured requirements from a PDF-derived markdown."""
    truncated = _truncate_for_prompt(parsed_markdown)
    prompt = (
        "Source RFI text (markdown):\n\n"
        f"{truncated}\n\n"
        "Produce the JSON described in the system prompt."
    )
    try:
        raw = await _generate(
            prompt,
            _EXTRACTION_SYSTEM,
            model=model,
            timeout=_EXTRACTION_TIMEOUT,
            num_predict=2048,
        )
    except Exception as exc:
        logger.error("Requirement extraction failed: %s", exc, exc_info=True)
        return {
            "title": "Untitled RFI",
            "summary": "",
            "requirements": [],
            "language": "en",
            "warning": f"Extraction failed: {exc}",
        }

    parsed = _safe_json_loads(raw)
    if not isinstance(parsed, dict):
        logger.warning("Extraction did not yield JSON object; returning empty payload")
        return {
            "title": "Untitled RFI",
            "summary": "",
            "requirements": [],
            "language": "en",
            "warning": "Extraction returned no structured data",
        }

    requirements_raw = parsed.get("requirements")
    if not isinstance(requirements_raw, list):
        requirements_raw = []

    requirements: list[dict[str, Any]] = []
    for idx, item in enumerate(requirements_raw, start=1):
        if not isinstance(item, dict):
            continue
        prompt_text = str(item.get("prompt") or item.get("question") or "").strip()
        if not prompt_text:
            continue
        category = str(item.get("category") or "general").strip().lower()
        if category not in {"projects", "engineers", "company", "general"}:
            category = "general"

        def _as_str_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v or "").strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        def _as_number(value: Any) -> float | None:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                match = re.search(r"-?\d+(?:\.\d+)?", value)
                if match:
                    try:
                        return float(match.group(0))
                    except ValueError:
                        return None
            return None

        requirements.append(
            {
                "id": str(item.get("id") or f"req-{idx}"),
                "category": category,
                "prompt": prompt_text,
                "projects": _as_str_list(item.get("projects")),
                "products": _as_str_list(item.get("products")),
                "min_experience_years": _as_number(item.get("min_experience_years")),
                "timeframe_years": _as_number(item.get("timeframe_years")),
            }
        )

    return {
        "title": str(parsed.get("title") or "Untitled RFI").strip()[:240] or "Untitled RFI",
        "summary": str(parsed.get("summary") or "").strip(),
        "requirements": requirements,
        "language": (str(parsed.get("language") or "en").strip().lower() or "en")[:8],
    }


_DRAFT_STREAM_FLUSH_CHARS = 8


class GenerationCancelled(Exception):
    """Raised when a draft generation is cancelled via stop-generation."""


def _draft_fallback_markdown(
    title: str,
    summary: str,
    requirements: list[Any],
) -> str:
    lines = [f"# {title}", ""]
    if summary:
        lines.extend([summary, ""])
    lines.append("> Automatic draft generation failed. Please edit this document manually.")
    for req in requirements:
        if isinstance(req, dict):
            prompt = req.get("prompt", "Requirement")
        else:
            prompt = str(req)
        lines.extend([f"\n## {prompt}\n", "<!-- TODO: write response -->"])
    return "\n".join(lines)


def _finalize_draft(raw: str, title: str) -> str:
    cleaned = _strip_code_fences(raw)
    if not cleaned.lstrip().startswith("#"):
        cleaned = f"# {title}\n\n{cleaned}"
    return cleaned.strip() + "\n"


async def _stream_ollama_markdown_completion(
    prompt: str,
    system: str,
    *,
    model: str | None,
    timeout: float,
    num_predict: int,
    flush_chars: int,
    on_chunk: Callable[[str], Awaitable[None]] | None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Stream Ollama /api/generate; accumulate full reply; optionally emit buffered deltas."""
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": True,
        "options": {"temperature": 0.2, "num_predict": num_predict},
    }
    accumulated = ""
    buffer = ""
    timeout_cfg = httpx.Timeout(timeout + 120.0, connect=120.0)
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        async with client.stream("POST", f"{OLLAMA_API}/api/generate", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                # Check for cancellation on every token
                if cancel_check and cancel_check():
                    logger.info("Draft generation cancelled during streaming")
                    raise GenerationCancelled("Generation was stopped by user")
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = data.get("response") or ""
                if token:
                    accumulated += token
                    buffer += token
                    while len(buffer) >= flush_chars:
                        slice_out = buffer[:flush_chars]
                        buffer = buffer[flush_chars:]
                        if on_chunk:
                            await on_chunk(slice_out)
                if data.get("done") is True:
                    break
    if buffer and on_chunk:
        await on_chunk(buffer)
        buffer = ""
    return accumulated


_DRAFT_SYSTEM = (
    "You are a senior proposal writer drafting an RFI response document.\n"
    "Write production-ready Markdown only. Do not include explanations.\n"
    "Rules:\n"
    "1. Always respond in Indonesian (Bahasa Indonesia), ensuring a highly professional tone ready to be sent to the customer.\n"
    "2. Keep each response concise (around 2-3 sentences per chapter or section) unless a table is required.\n"
    "3. Make every paragraph detailed and strictly answer only what is asked for in the requirements.\n"
    "4. Use clear section headings (## and ###).\n"
    "5. For every requirement provide a direct response. If data is missing,\n"
    "   leave a placeholder like `<!-- INSERT: project name -->` so a human can fill it.\n"
    "6. Where the requirement asks for projects or engineers, add a Markdown table\n"
    "   with the columns the requirement asks for (e.g. Project Name, Customer, Year, Products).\n"
    "7. Never invent facts. Use placeholders instead.\n"
    "8. Do not wrap your answer in code fences.\n"
)


async def draft_response_markdown(
    parsed_markdown: str,
    extraction: dict[str, Any],
    *,
    model: str | None = None,
    on_stream_delta: Callable[[str], Awaitable[None]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Use the LLM to draft markdown. Optional ``on_stream_delta`` receives streamed chunks."""
    truncated = _truncate_for_prompt(parsed_markdown)
    requirements = extraction.get("requirements") or []
    title = extraction.get("title") or "RFI Response"
    summary = extraction.get("summary") or ""

    prompt_lines = [
        f"Title: {title}",
        f"Summary: {summary}" if summary else "",
        "",
        "Source document (markdown):",
        truncated,
        "",
        "Structured requirements (JSON):",
        json.dumps(requirements, ensure_ascii=False, indent=2),
        "",
        "Write the complete response Markdown now. Begin with `# {title}` and respond to every requirement.",
    ]
    prompt = "\n".join(line for line in prompt_lines if line is not None)

    try:
        raw = await _stream_ollama_markdown_completion(
            prompt,
            _DRAFT_SYSTEM,
            model=model,
            timeout=_DRAFT_TIMEOUT,
            num_predict=4096,
            flush_chars=_DRAFT_STREAM_FLUSH_CHARS,
            on_chunk=on_stream_delta,
            cancel_check=cancel_check,
        )
    except GenerationCancelled:
        raise  # Re-raise so pipeline can handle partial content
    except Exception as exc:
        logger.error("Draft generation failed: %s", exc, exc_info=True)
        return _draft_fallback_markdown(
            title,
            summary,
            list(requirements) if isinstance(requirements, list) else [],
        )

    return _finalize_draft(raw, title)
