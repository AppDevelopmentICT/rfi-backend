"""
Docling integration for PDF → Markdown.

- **Remote** (default): [docling-serve](https://github.com/docling-project/docling-serve) HTTP API,
  aligned with https://docling-project.github.io/docling/ — prefer JSON
  ``POST /v1/convert/source`` (or legacy ``POST /v1alpha/convert/source`` when only the prototype routes exist).
  When ``DOCLING_SERVE_API_SEGMENT=auto`` (default), tries ``/v1`` then ``/v1alpha``.
  When set to ``v1alpha`` only, tries ``/v1alpha`` first then ``/v1`` so v1-only docling-serve still works.
  When no path prefix is configured, also tries sensible URL bases (bare host plus ``HOST/api``, and strips
  a trailing ``/api`` on ``DOCLING_API`` once to probe the parent if paths were mismatched).
  Optionally add comma-separated ``DOCLING_SERVE_EXTRA_PATH_PREFIXES`` when docling sits under a custom path.
  ``DOCLING_API`` is normalized to strip trailing ``/v1`` or ``/v1alpha`` so paths are not doubled.
  If docling-serve returns 404 ``Task result not found`` from the synchronous converters (RQ/redis),
  retries use POST ``…/convert/*/async``, ``GET …/status/poll/{task_id}``, then ``GET …/result/{task_id}``.
- **Embedded**: Official [Docling Python library](https://docling-project.github.io/docling/)
  (optional install ``requirements-docling-embedded.txt``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from app.config import (
    DOCLING_ABORT_ON_ERROR,
    DOCLING_ASYNC_LITE_FALLBACK,
    DOCLING_ASYNC_POLL_INTERVAL,
    DOCLING_API,
    DOCLING_API_KEY,
    DOCLING_AUTO_RETRY_FORCE_OCR,
    DOCLING_DOCUMENT_TIMEOUT,
    DOCLING_FORCE_OCR,
    DOCLING_MODE,
    DOCLING_OCR_ENGINE,
    DOCLING_OCR_LANG,
    DOCLING_OCR_PRESET,
    DOCLING_PDF_BACKEND,
    DOCLING_REMOTE_TRANSPORT,
    DOCLING_SERVE_API_SEGMENT,
    DOCLING_SERVE_EXTRA_PATH_PREFIXES,
    DOCLING_SERVE_PATH_PREFIX,
    DOCLING_TABLE_MODE,
)
from app.services.external.docling_embedded import try_convert_pdf_bytes
from app.services.rfi.pdf_native_text import extract_pdf_native_text_plain

logger = logging.getLogger(__name__)

_LEGACY_V1ALPHA_PDF_BACKENDS = frozenset({"pypdfium2", "dlparse_v1", "dlparse_v2", "dlparse_v4"})

_NATIVE_FALLBACK_NOTE = (
    "> **Note:** Text below was recovered from the PDF’s native text layer (fallback) "
    "because structured conversion yielded too little selectable content.\n\n"
)


def _alphabetic_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def _looks_like_weak_extraction(markdown: str) -> bool:
    """Heuristic for scanned PDFs, broken glyph layers, or empty Docling output."""
    t = (markdown or "").strip()
    if len(t) < 48:
        return True
    letters = _alphabetic_count(t)
    if letters < 160:
        return True
    digits = sum(1 for ch in t if ch.isdigit())
    ratio = (letters + digits) / max(len(t), 1)
    if ratio < 0.07 and letters < 520:
        return True
    return False


def _serve_options(force_ocr: bool) -> dict[str, Any]:
    """Options for ConvertDocumentsOptions (docling-core / docling-serve v1)."""
    return {
        "from_formats": ["pdf"],
        "to_formats": ["md"],
        "image_export_mode": "embedded",
        "do_ocr": True,
        "force_ocr": force_ocr,
        # v1 prefers ocr_preset; ocr_engine is deprecated and can trigger validation issues on strict servers.
        "ocr_preset": DOCLING_OCR_PRESET,
        "ocr_lang": list(DOCLING_OCR_LANG) if DOCLING_OCR_LANG else None,
        "pdf_backend": DOCLING_PDF_BACKEND,
        "table_mode": DOCLING_TABLE_MODE,
        "abort_on_error": DOCLING_ABORT_ON_ERROR,
        "do_table_structure": True,
    }


def _serve_options_compat_multipart(force_ocr: bool) -> dict[str, Any]:
    """Flatten-friendly copy; multipart handlers often still expect ``ocr_engine``."""
    o = _serve_options(force_ocr)
    o["ocr_engine"] = DOCLING_OCR_ENGINE
    return o


def _legacy_v1alpha_pdf_backend() -> str:
    """``docling_parse`` is v1-only; v1alpha expects ``dlparse_*`` names."""
    b = DOCLING_PDF_BACKEND
    if b in _LEGACY_V1ALPHA_PDF_BACKENDS:
        return b
    return "dlparse_v4"


def _serve_options_legacy_v1alpha(force_ocr: bool) -> dict[str, Any]:
    """convert/options shape for docling-serve ``/v1alpha/`` (no ``ocr_preset``)."""
    return {
        "from_formats": ["pdf"],
        "to_formats": ["md"],
        "image_export_mode": "embedded",
        "do_ocr": True,
        "force_ocr": force_ocr,
        "ocr_engine": DOCLING_OCR_ENGINE,
        "ocr_lang": list(DOCLING_OCR_LANG) if DOCLING_OCR_LANG else None,
        "pdf_backend": _legacy_v1alpha_pdf_backend(),
        "table_mode": DOCLING_TABLE_MODE,
        "abort_on_error": DOCLING_ABORT_ON_ERROR,
        "do_table_structure": True,
    }


def _docling_unique_ordered(urls: list[str]) -> tuple[str, ...]:
    """Deduplicate while preserving probe order."""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        b = u.rstrip("/")
        if not b or b in seen:
            continue
        seen.add(b)
        out.append(b)
    return tuple(out)


def _docling_api_root_candidates() -> tuple[str, ...]:
    """Bases under which ``/{v1|v1alpha}/convert/*`` exists.

    Covers: bare docling root, gateway ``/api``, duplicate ``DOCLING_API=/…/api`` with parent-root fallback,
    and optional ``DOCLING_SERVE_EXTRA_PATH_PREFIXES`` for uncommon mounts.
    """
    root = DOCLING_API.rstrip("/")
    manual = DOCLING_SERVE_PATH_PREFIX.strip("/")
    if manual:
        return _docling_unique_ordered([f"{root}/{manual}".rstrip("/")])

    ordered: list[str] = []
    rl = root.lower()

    ordered.append(root)
    if rl.endswith("/api"):
        parent = root.rsplit("/", 1)[0].rstrip("/")
        if parent != root.rstrip("/") and parent:
            ordered.append(parent)
    elif not rl.endswith("/api"):
        ordered.append(f"{root}/api")

    for seg in DOCLING_SERVE_EXTRA_PATH_PREFIXES:
        ordered.append(f"{root}/{seg}".rstrip("/"))

    # If extras were configured under ".../api" root, attempt same suffixes relative to stripped parent once.
    if rl.endswith("/api"):
        parent = root.rsplit("/", 1)[0].rstrip("/")
        if parent:
            ordered.extend(f"{parent}/{seg}".rstrip("/") for seg in DOCLING_SERVE_EXTRA_PATH_PREFIXES)

    return _docling_unique_ordered(ordered)


def _api_segment_candidates() -> tuple[str, ...]:
    s = DOCLING_SERVE_API_SEGMENT
    if s == "v1":
        return ("v1",)
    if s == "v1alpha":
        # Prefer legacy prefix first; modern docling-serve is v1-only for async/sync — fall back so a mis-set
        # DOCLING_SERVE_API_SEGMENT does not deadlock on HTTP 404 for every …/convert/*/async route.
        return ("v1alpha", "v1")
    if s in ("auto", ""):
        return ("v1", "v1alpha")
    logger.warning("Unknown DOCLING_SERVE_API_SEGMENT=%s; using auto [v1, v1alpha]", s)
    return ("v1", "v1alpha")


def _missing_route_error(exc: BaseException) -> bool:
    """Wrong path / method — exclude docling misleading ``Task result not found`` HTTP 404."""
    msg = str(exc).lower()
    if "task result not found" in msg or "completion status" in msg:
        return False
    return "404" in str(exc) or "405" in str(exc)


def _sync_convert_file_unreachable(exc: BaseException) -> bool:
    """Many docling-serve builds omit sync ``POST …/convert/file`` but expose async enqueue/poll/result."""
    if not _missing_route_error(exc):
        return False
    return "convert/file" in str(exc).lower()


def _async_enqueue_json_route_missing(status_code: int) -> bool:
    """Some deployments only mount multipart async (``/convert/file/async``), not JSON ``/convert/source/async``."""
    return status_code in (404, 405, 422)


def _http_extra_headers() -> dict[str, str]:
    hdr: dict[str, str] = {}
    key = DOCLING_API_KEY.strip()
    if key:
        hdr["X-Api-Key"] = key
    return hdr


def _detail_from_response_text(response: httpx.Response) -> str:
    raw = (response.text or "").strip()
    if not raw:
        return ""
    try:
        parsed: Any = response.json()
        if isinstance(parsed, dict) and "detail" in parsed:
            return str(parsed.get("detail"))
        return raw[:1600]
    except ValueError:
        return raw[:1600]


def _raise_docling_http(label: str, response: httpx.Response) -> None:
    detail = _detail_from_response_text(response)
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"Docling {label} HTTP {response.status_code}{suffix}")


def _elicitable_for_async_fallback(exc: BaseException) -> bool:
    """docling-serve sync path can return 404 when RQ/Redis has no stored result; async job API may still work."""
    msg = str(exc).lower()
    return "task result not found" in msg or "completion status" in msg


def _form_pairs_to_mapping(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Flatten duplicate keys into list values — required for httpx ``AsyncClient`` multipart (data must be a Mapping)."""
    acc: dict[str, list[str]] = {}
    for k, v in pairs:
        acc.setdefault(k, []).append(v)
    out: dict[str, Any] = {}
    for k, vals in acc.items():
        out[k] = vals if len(vals) > 1 else vals[0]
    return out


def _build_multipart_fields(
    *, force_ocr: bool, route_segment: str, lite: bool = False
) -> list[tuple[str, str]]:
    """Flatten options for ``POST /{v1|v1alpha}/convert/file`` multipart uploads."""
    if route_segment == "v1alpha":
        o = dict(_serve_options_legacy_v1alpha(force_ocr))
        include_ocr_preset = False
    else:
        o = dict(_serve_options_compat_multipart(force_ocr))
        include_ocr_preset = True
    if lite:
        o["do_table_structure"] = False
        o["abort_on_error"] = False
    fields: list[tuple[str, str]] = []
    for fmt in o.get("from_formats") or []:
        fields.append(("from_formats", str(fmt)))
    for fmt in o.get("to_formats") or []:
        fields.append(("to_formats", str(fmt)))
    fields.extend(
        [
            ("image_export_mode", str(o["image_export_mode"])),
            ("do_ocr", "true" if o["do_ocr"] else "false"),
            ("force_ocr", "true" if o["force_ocr"] else "false"),
            ("ocr_engine", str(o["ocr_engine"])),
        ]
    )
    if include_ocr_preset:
        fields.append(("ocr_preset", str(o["ocr_preset"])))
    fields.extend(
        [
            ("pdf_backend", str(o["pdf_backend"])),
            ("table_mode", str(o["table_mode"])),
            ("abort_on_error", str(o["abort_on_error"]).lower()),
            ("do_table_structure", "true" if o.get("do_table_structure") else "false"),
        ]
    )
    for lang in o.get("ocr_lang") or []:
        fields.append(("ocr_lang", str(lang)))
    return fields


def _markdown_from_payload(result: Any) -> str | None:
    if isinstance(result, dict):
        doc = result.get("document")
        if isinstance(doc, dict):
            md = doc.get("md_content") or doc.get("markdown")
            if isinstance(md, str) and md.strip():
                return md
        results_list = result.get("results")
        if isinstance(results_list, list) and results_list:
            for r in results_list:
                if not isinstance(r, dict):
                    continue
                inner_doc = r.get("document")
                if isinstance(inner_doc, dict):
                    md = inner_doc.get("md_content") or inner_doc.get("markdown")
                    if isinstance(md, str) and md.strip():
                        return md
                content = r.get("markdown") or r.get("text")
                if isinstance(content, str) and content.strip():
                    return content
        md = result.get("markdown")
        if isinstance(md, str) and md.strip():
            return md
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _normalize_docling_payload(payload: Any) -> str:
    md_out = _markdown_from_payload(payload)
    if md_out:
        return md_out
    logger.warning("Docling JSON lacked markdown/text fields; returning stringified payload snippet")
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload)
    return str(payload)


def _strip_none(opts: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in opts.items() if v is not None}


def _options_payload_for_convert(
    force_ocr: bool, route_segment: str, *, lite: bool = False
) -> dict[str, Any]:
    raw = (
        _serve_options_legacy_v1alpha(force_ocr)
        if route_segment == "v1alpha"
        else _serve_options(force_ocr)
    )
    opts = dict(raw)
    if lite:
        opts["do_table_structure"] = False
        opts["abort_on_error"] = False
    return _strip_none(opts)


def _ordered_source_json_bodies(
    force_ocr: bool,
    route_segment: str,
    *,
    b64: str,
    filename: str,
    lite: bool = False,
) -> list[dict[str, Any]]:
    opts = _options_payload_for_convert(force_ocr, route_segment, lite=lite)
    if route_segment == "v1alpha":
        return [
            {"options": opts, "file_sources": [{"base64_string": b64, "filename": filename}]},
        ]
    return [
        {
            "options": opts,
            "sources": [{"kind": "file", "base64_string": b64, "filename": filename}],
        },
        {"options": opts, "file_sources": [{"base64_string": b64, "filename": filename}]},
    ]


async def _poll_docling_task_until_done(
    client: httpx.AsyncClient,
    api_root: str,
    route_segment: str,
    task_id: str,
) -> dict[str, Any]:
    hdr = _http_extra_headers()
    poll_url = f"{api_root.rstrip('/')}/{route_segment}/status/poll/{task_id}"
    deadline = time.monotonic() + DOCLING_DOCUMENT_TIMEOUT
    last_task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        pr = await client.get(poll_url, headers=hdr or None)
        if pr.status_code != 200:
            _raise_docling_http(f"{route_segment}/status/poll/{task_id}", pr)
        try:
            parsed: Any = pr.json()
        except ValueError:
            await asyncio.sleep(max(0.5, DOCLING_ASYNC_POLL_INTERVAL))
            continue
        if isinstance(parsed, dict):
            last_task = parsed
        status = ""
        if isinstance(parsed, dict):
            status = str(parsed.get("task_status") or parsed.get("status") or "").lower()
        if status in ("success", "failure"):
            return last_task if isinstance(parsed, dict) else {}
        await asyncio.sleep(max(0.5, DOCLING_ASYNC_POLL_INTERVAL))
    raise RuntimeError(
        f"Docling async task {task_id!r} polling timed out after {DOCLING_DOCUMENT_TIMEOUT}s"
    )


async def _try_get_async_result_json_optional(
    client: httpx.AsyncClient,
    api_root: str,
    route_segment: str,
    task_id: str,
) -> Any | None:
    url = f"{api_root.rstrip('/')}/{route_segment}/result/{task_id}"
    result_r = await client.get(url, headers=_http_extra_headers() or None)
    if result_r.status_code != 200:
        return None
    try:
        return result_r.json()
    except ValueError:
        return None


def _recover_markdown_if_any(payload: Any) -> str | None:
    """Prefer real markdown from a task result payload; ignore error-only JSON or stringified fallbacks."""
    if payload is None:
        return None
    extracted = _markdown_from_payload(payload)
    if extracted and extracted.strip():
        return extracted.strip()
    return None


def _format_async_task_failure(task_summary: dict[str, Any]) -> str:
    parts: list[str] = []
    em = task_summary.get("error_message")
    if isinstance(em, str) and em.strip():
        parts.append(em.strip())
    errs = task_summary.get("errors")
    if errs is not None:
        parts.append(f"errors={errs!r}")
    meta = task_summary.get("task_meta")
    if meta is not None:
        parts.append(f"task_meta={meta!r}")
    if not parts:
        parts.append("no error_message in poll payload (see docling-serve worker/GPU logs)")
    return "; ".join(parts)


async def _recover_markdown_after_async_failure(
    client: httpx.AsyncClient,
    api_root: str,
    route_segment: str,
    task_id: str,
) -> str | None:
    payload = await _try_get_async_result_json_optional(client, api_root, route_segment, task_id)
    return _recover_markdown_if_any(payload)


async def _fetch_async_job_result_document(
    client: httpx.AsyncClient,
    api_root: str,
    route_segment: str,
    task_id: str,
) -> Any:
    url = f"{api_root.rstrip('/')}/{route_segment}/result/{task_id}"
    hdr = _http_extra_headers()
    result_r = await client.get(url, headers=hdr or None)
    if result_r.status_code != 200:
        _raise_docling_http(f"{route_segment}/result/{task_id}", result_r)
    try:
        return result_r.json()
    except ValueError:
        return result_r.text or ""


async def _convert_via_async_endpoint_chain(
    file_bytes: bytes,
    filename: str,
    *,
    api_root: str,
    force_ocr: bool,
    route_segment: str,
    client: httpx.AsyncClient,
    lite: bool = False,
) -> str:
    """Use ``POST …/convert/(source|file)/async`` + poll + ``GET …/result/{task_id}`` (RQ-backed docling-serve)."""
    b64 = base64.b64encode(file_bytes).decode("ascii")
    name = filename or "upload.pdf"
    hdr = {"Content-Type": "application/json", "Accept": "application/json"}
    hdr.update(_http_extra_headers())
    bodies = _ordered_source_json_bodies(
        force_ocr, route_segment, b64=b64, filename=name, lite=lite
    )
    submit_url_base = f"{api_root.rstrip('/')}/{route_segment}/convert/source/async"
    hdr_extra = _http_extra_headers() or None
    for idx, body in enumerate(bodies):
        sub = await client.post(submit_url_base, json=body, headers=hdr)
        if sub.status_code == 422 and idx + 1 < len(bodies):
            logger.warning(
                "Docling async %s enqueue returned 422; retrying next JSON body variant",
                route_segment,
            )
            continue
        if _async_enqueue_json_route_missing(sub.status_code):
            logger.warning(
                "Docling %s/convert/source/async returned %s (route may be absent); "
                "trying %s/convert/file/async",
                route_segment,
                sub.status_code,
                route_segment,
            )
            break
        if sub.status_code not in (200, 201):
            _raise_docling_http(f"{route_segment}/convert/source/async", sub)
        job = sub.json()
        if not isinstance(job, dict) or "task_id" not in job:
            raise RuntimeError(f"Docling async enqueue returned unexpected JSON: {job!r}")
        tid = str(job["task_id"])
        task_final = await _poll_docling_task_until_done(client, api_root, route_segment, tid)
        st = str(task_final.get("task_status") or task_final.get("status") or "").lower()
        if st == "failure":
            recovered = await _recover_markdown_after_async_failure(
                client, api_root, route_segment, tid
            )
            if recovered:
                logger.warning(
                    "Docling async JSON task failure on %s (variant %s) but recovered markdown from "
                    "/result; using recovered text (%s)",
                    route_segment,
                    idx,
                    _format_async_task_failure(task_final),
                )
                return recovered
            logger.warning(
                "Docling async JSON %s variant %s reported failure (%s); trying next enqueue variant",
                route_segment,
                idx,
                _format_async_task_failure(task_final),
            )
            continue
        payload_any = await _fetch_async_job_result_document(client, api_root, route_segment, tid)
        return _normalize_docling_payload(payload_any)

    mul_url = f"{api_root.rstrip('/')}/{route_segment}/convert/file/async"
    files = {"files": (name, file_bytes, "application/pdf")}
    multipart = _form_pairs_to_mapping(
        _build_multipart_fields(force_ocr=force_ocr, route_segment=route_segment, lite=lite)
    )
    logger.info(
        "Docling async multipart submit %s for %s (JSON enqueue variants exhausted)",
        route_segment,
        name,
    )
    sub_m = await client.post(mul_url, files=files, data=multipart, headers=hdr_extra)
    if sub_m.status_code not in (200, 201):
        _raise_docling_http(f"{route_segment}/convert/file/async", sub_m)
    job_m = sub_m.json()
    if not isinstance(job_m, dict) or "task_id" not in job_m:
        raise RuntimeError(f"Docling async multipart enqueue unexpected: {job_m!r}")
    tid_m = str(job_m["task_id"])
    polled = await _poll_docling_task_until_done(client, api_root, route_segment, tid_m)
    pol_st = str(polled.get("task_status") or polled.get("status") or "").lower()
    if pol_st == "failure":
        recovered_m = await _recover_markdown_after_async_failure(
            client, api_root, route_segment, tid_m
        )
        if recovered_m:
            logger.warning(
                "Docling async multipart task failure on %s but recovered markdown from /result; using it (%s)",
                route_segment,
                _format_async_task_failure(polled),
            )
            return recovered_m
        raise RuntimeError(
            f"Docling async multipart conversion failed ({route_segment}): "
            f"{_format_async_task_failure(polled)}"
        )
    payload_m = await _fetch_async_job_result_document(client, api_root, route_segment, tid_m)
    return _normalize_docling_payload(payload_m)


async def _convert_via_async_with_optional_lite_fallback(
    file_bytes: bytes,
    filename: str,
    *,
    api_root: str,
    force_ocr: bool,
    route_segment: str,
    client: httpx.AsyncClient,
) -> str:
    """Run async JSON→multipart chain; optionally retry once with lighter table/abort settings."""
    try:
        return await _convert_via_async_endpoint_chain(
            file_bytes,
            filename,
            api_root=api_root,
            force_ocr=force_ocr,
            route_segment=route_segment,
            client=client,
            lite=False,
        )
    except RuntimeError as first_exc:
        if not DOCLING_ASYNC_LITE_FALLBACK:
            raise
        logger.warning(
            "Docling async %s failed (%s); retrying with lite options "
            "(do_table_structure=false, abort_on_error=false)",
            route_segment,
            first_exc,
        )
        return await _convert_via_async_endpoint_chain(
            file_bytes,
            filename,
            api_root=api_root,
            force_ocr=force_ocr,
            route_segment=route_segment,
            client=client,
            lite=True,
        )



async def _convert_via_source_api(
    file_bytes: bytes,
    filename: str,
    *,
    api_root: str,
    force_ocr: bool,
    route_segment: str,
    client: httpx.AsyncClient,
) -> str:
    """JSON ``POST /{segment}/convert/source`` — ``v1`` (``sources``) or ``v1alpha`` (``file_sources``)."""
    url = f"{api_root.rstrip('/')}/{route_segment}/convert/source"
    opts = _options_payload_for_convert(force_ocr, route_segment)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    name = filename or "upload.pdf"

    hdr = {"Content-Type": "application/json", "Accept": "application/json"}
    hdr.update(_http_extra_headers())

    async def _post(body: dict[str, Any]) -> httpx.Response:
        return await client.post(url, json=body, headers=hdr)

    if route_segment == "v1alpha":
        response = await _post(
            {
                "options": opts,
                "file_sources": [{"base64_string": b64, "filename": name}],
            }
        )
    else:
        body_v1 = {
            "options": opts,
            "sources": [{"kind": "file", "base64_string": b64, "filename": name}],
        }
        response = await _post(body_v1)
        if response.status_code == 422:
            body_legacy = {
                "options": opts,
                "file_sources": [{"base64_string": b64, "filename": name}],
            }
            logger.warning(
                "Docling %s/convert/source returned 422 (%s…); retrying legacy file_sources JSON",
                route_segment,
                response.text[:200],
            )
            response = await _post(body_legacy)

    if response.status_code != 200:
        _raise_docling_http(f"{route_segment}/convert/source", response)
    try:
        payload: Any = response.json()
    except ValueError:
        return response.text or ""
    return _normalize_docling_payload(payload)


async def _convert_via_multipart(
    file_bytes: bytes,
    filename: str,
    *,
    api_root: str,
    force_ocr: bool,
    route_segment: str,
    client: httpx.AsyncClient,
) -> str:
    """Multipart ``POST /{segment}/convert/file``."""
    url = f"{api_root.rstrip('/')}/{route_segment}/convert/file"
    files = {"files": (filename or "upload.pdf", file_bytes, "application/pdf")}
    multipart = _form_pairs_to_mapping(
        _build_multipart_fields(force_ocr=force_ocr, route_segment=route_segment)
    )
    hdr = _http_extra_headers()
    backends = DOCLING_PDF_BACKEND if route_segment == "v1" else _legacy_v1alpha_pdf_backend()
    preset = "-" if route_segment == "v1alpha" else DOCLING_OCR_PRESET
    logger.info(
        "Docling multipart %s/%s %s backend=%s ocr_preset=%s",
        route_segment,
        "convert/file",
        filename,
        backends,
        preset,
    )
    response = await client.post(url, files=files, data=multipart, headers=hdr or None)
    if response.status_code != 200:
        _raise_docling_http(f"{route_segment}/convert/file", response)
    try:
        payload: Any = response.json()
    except ValueError:
        return response.text or ""
    return _normalize_docling_payload(payload)


async def _convert_json_then_multipart_on_http_mismatch(
    file_bytes: bytes,
    filename: str,
    *,
    api_root: str,
    force_ocr: bool,
    route_segment: str,
    client: httpx.AsyncClient,
) -> str:
    """Try synchronous JSON → multipart; on RQ \"task result\" 404 skip redundant multipart (same failure) use async."""
    try:
        return await _convert_via_source_api(
            file_bytes,
            filename,
            api_root=api_root,
            force_ocr=force_ocr,
            route_segment=route_segment,
            client=client,
        )
    except RuntimeError as exc:
        if _elicitable_for_async_fallback(exc):
            logger.warning(
                "Docling sync /convert/source hit RQ/redis-style 404 (%s); skipping sync multipart retry; "
                "using async convert + poll APIs",
                exc,
            )
            return await _convert_via_async_with_optional_lite_fallback(
                file_bytes,
                filename,
                api_root=api_root,
                force_ocr=force_ocr,
                route_segment=route_segment,
                client=client,
            )
        if any(code in str(exc) for code in ("404", "405", "422")):
            logger.warning(
                "Docling %s/convert/source failed (%s); trying multipart %s/convert/file",
                route_segment,
                exc,
                route_segment,
            )
            try:
                return await _convert_via_multipart(
                    file_bytes,
                    filename,
                    api_root=api_root,
                    force_ocr=force_ocr,
                    route_segment=route_segment,
                    client=client,
                )
            except RuntimeError as mex:
                if _elicitable_for_async_fallback(mex) or _sync_convert_file_unreachable(mex):
                    logger.warning(
                        "Docling sync multipart failed (%s); trying async convert APIs",
                        mex,
                    )
                    return await _convert_via_async_with_optional_lite_fallback(
                        file_bytes,
                        filename,
                        api_root=api_root,
                        force_ocr=force_ocr,
                        route_segment=route_segment,
                        client=client,
                    )
                raise
        raise


async def _try_segments_under_root(
    file_bytes: bytes,
    filename: str,
    *,
    api_root: str,
    force_ocr: bool,
    transport: str,
    client: httpx.AsyncClient,
) -> str:
    segments = _api_segment_candidates()
    if transport == "multipart":
        last_mp: RuntimeError | None = None
        for i, route_segment in enumerate(segments):
            try:
                return await _convert_via_multipart(
                    file_bytes,
                    filename,
                    api_root=api_root,
                    force_ocr=force_ocr,
                    route_segment=route_segment,
                    client=client,
                )
            except RuntimeError as exc:
                last_mp = exc
                if _elicitable_for_async_fallback(exc) or _sync_convert_file_unreachable(exc):
                    logger.warning(
                        "Docling sync multipart (%s): %s; trying async convert chain",
                        route_segment,
                        exc,
                    )
                    try:
                        return await _convert_via_async_with_optional_lite_fallback(
                            file_bytes,
                            filename,
                            api_root=api_root,
                            force_ocr=force_ocr,
                            route_segment=route_segment,
                            client=client,
                        )
                    except RuntimeError as aexc:
                        last_mp = aexc
                        if i < len(segments) - 1 and _missing_route_error(last_mp):
                            logger.warning(
                                "Docling async after missing sync multipart (%s): %s; trying %s",
                                route_segment,
                                last_mp,
                                segments[i + 1],
                            )
                            continue
                        raise
                if i < len(segments) - 1 and _missing_route_error(exc):
                    logger.warning(
                        "Docling multipart %s not available (%s); trying %s",
                        route_segment,
                        exc,
                        segments[i + 1],
                    )
                    continue
                raise
        assert last_mp is not None
        raise last_mp

    last_err: RuntimeError | None = None
    for i, route_segment in enumerate(segments):
        try:
            return await _convert_json_then_multipart_on_http_mismatch(
                file_bytes,
                filename,
                api_root=api_root,
                force_ocr=force_ocr,
                route_segment=route_segment,
                client=client,
            )
        except RuntimeError as exc:
            last_err = exc
            if i < len(segments) - 1 and _missing_route_error(exc):
                logger.warning(
                    "Docling API prefix %s not available (%s); trying %s",
                    route_segment,
                    exc,
                    segments[i + 1],
                )
                continue
            raise
    assert last_err is not None
    raise last_err


async def _remote_convert_roundtrip(file_bytes: bytes, filename: str, *, force_ocr: bool) -> str:
    transport = DOCLING_REMOTE_TRANSPORT.lower().strip()
    timeout = httpx.Timeout(DOCLING_DOCUMENT_TIMEOUT, connect=min(120.0, DOCLING_DOCUMENT_TIMEOUT))
    roots = _docling_api_root_candidates()
    logger.info(
        "Docling serve candidate bases for %s (%s transport): %s",
        filename or "PDF",
        transport,
        "; ".join(roots),
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        last_root_exc: RuntimeError | None = None
        for bi, api_root in enumerate(roots):
            try:
                return await _try_segments_under_root(
                    file_bytes,
                    filename,
                    api_root=api_root,
                    force_ocr=force_ocr,
                    transport=transport,
                    client=client,
                )
            except RuntimeError as exc:
                last_root_exc = exc
                if bi < len(roots) - 1 and _missing_route_error(exc):
                    logger.warning(
                        "Docling base %s unreachable or wrong mount (%s); trying next candidate",
                        api_root,
                        exc,
                    )
                    continue
                if _missing_route_error(exc) and roots:
                    raise RuntimeError(
                        "Docling returned HTTP 404/405 on every converter path under all bases "
                        f"{list(roots)}; nothing matched /{{v1|v1alpha}}/convert/*. "
                        "Confirm docling-serve is listening on DOCLING_API (default port 5001; try "
                        "curl \"$DOCLING_API/v1/convert/source\" or `/api/v1/convert/source` behind gateways). "
                        "For custom mounts set DOCLING_SERVE_PATH_PREFIX or comma-separated "
                        "DOCLING_SERVE_EXTRA_PATH_PREFIXES. "
                        f"Underlying error: {exc}"
                    ) from exc
                raise
        if not roots:
            raise RuntimeError(
                "DOCLING_API produced no convertible base URLs; check DOCLING_API and optional path prefixes."
            )
        assert last_root_exc is not None
        raise last_root_exc


async def _embedded_convert(file_bytes: bytes, filename: str, *, force_ocr: bool) -> str | None:
    """Run Docling in-process when the Python package is installed."""
    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        None,
        lambda: try_convert_pdf_bytes(
            file_bytes,
            filename,
            ocr_lang=DOCLING_OCR_LANG,
            force_full_page_ocr=force_ocr,
        ),
    )


async def parse_document(file_bytes: bytes, filename: str) -> str:
    """PDF → Markdown via Docling (embedded and/or docling-serve) plus native-text fallback."""
    mode = DOCLING_MODE.lower().strip()
    markdown = ""
    used_embedded = False

    if mode in ("embedded", "embedded_then_remote"):
        try:
            primary_force = DOCLING_FORCE_OCR
            emb = await _embedded_convert(file_bytes, filename, force_ocr=primary_force)
            if emb is not None and emb.strip():
                used_embedded = True
                markdown = emb
                if (
                    DOCLING_AUTO_RETRY_FORCE_OCR
                    and not primary_force
                    and _looks_like_weak_extraction(markdown)
                ):
                    emb2 = await _embedded_convert(file_bytes, filename, force_ocr=True)
                    if (
                        emb2
                        and emb2.strip()
                        and _alphabetic_count(emb2) > _alphabetic_count(markdown)
                    ):
                        markdown = emb2
        except Exception as exc:
            logger.warning("Embedded Docling failed for %s: %s", filename, exc)
            markdown = ""

        if mode == "embedded":
            if not (markdown or "").strip():
                raise RuntimeError(
                    "DOCLING_MODE=embedded requires the Docling Python package "
                    "(see requirements-docling-embedded.txt)."
                )

        elif mode == "embedded_then_remote" and markdown and not _looks_like_weak_extraction(markdown):
            # Good enough extraction from local Docling — skip HTTP.
            final = markdown
            return await _postprocess_native_fallback(file_bytes, filename, final)

    if mode in ("remote", "embedded_then_remote") and not (
        mode == "embedded_then_remote"
        and used_embedded
        and markdown.strip()
        and not _looks_like_weak_extraction(markdown)
    ):
        primary_force = DOCLING_FORCE_OCR
        try:
            markdown = await _remote_convert_roundtrip(file_bytes, filename, force_ocr=primary_force)

            if (
                DOCLING_AUTO_RETRY_FORCE_OCR
                and not primary_force
                and _looks_like_weak_extraction(markdown)
            ):
                try:
                    retry_md = await _remote_convert_roundtrip(
                        file_bytes, filename, force_ocr=True
                    )
                    if _alphabetic_count(retry_md) > _alphabetic_count(markdown):
                        logger.info("Docling OCR retry improved extraction for %s", filename)
                        markdown = retry_md
                except Exception as exc:
                    logger.warning("Docling force_ocr retry failed for %s: %s", filename, exc)
        except Exception as exc:
            logger.error("Docling remote conversion failed for %s: %s. Falling back to native PDF text.", filename, exc)
            markdown = ""

    return await _postprocess_native_fallback(file_bytes, filename, markdown)


async def _postprocess_native_fallback(
    file_bytes: bytes,
    filename: str,
    markdown: str,
) -> str:
    native_plain = extract_pdf_native_text_plain(file_bytes)
    if native_plain.strip():
        native_letters = _alphabetic_count(native_plain)
        md_letters = _alphabetic_count(markdown)
        substantially_richer = native_letters > max(
            int(md_letters * 1.35),
            md_letters + 400,
        )
        if substantially_richer or (
            _looks_like_weak_extraction(markdown) and native_letters > md_letters
        ):
            logger.info(
                "Using native PDF text layer for %s (letters_native=%s, letters_md=%s)",
                filename,
                native_letters,
                md_letters,
            )
            return _NATIVE_FALLBACK_NOTE + native_plain.strip()

    if _looks_like_weak_extraction(markdown):
        logger.warning(
            "PDF %s converted with sparse text; set DOCLING_FORCE_OCR=true or check scan quality.",
            filename,
        )

    return markdown
