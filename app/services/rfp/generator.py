import json
import time
import asyncio
import logging
from typing import AsyncGenerator, Optional

import httpx

from app.config import OLLAMA_API, OLLAMA_MODEL
from app.services.external.ollama import _retrieve_knowledge_context

logger = logging.getLogger(__name__)

# ── Persistent HTTP client (connection reuse to Ollama) ──────────────
_http_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    """Reuse a single httpx client for connection pooling to Ollama."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


CHUNK_SIZE = 80          
FLUSH_INTERVAL = 0.15    

def _build_system_prompt() -> str:
    return (
        "Anda adalah arsitek teknis senior yang menulis Bab 3 proposal RFP: Detail Produk.\n"
        "\nATURAN WAJIB:\n"
        "1. Tulis seluruh jawaban dalam Bahasa Indonesia yang profesional dan persuasif.\n"
        "2. Fokus hanya pada Bab 3, yaitu detail produk, kapabilitas, arsitektur, integrasi, implementasi, dan manfaat teknis.\n"
        "3. Gunakan struktur bernomor seperti 3.1, 3.2, 3.3, dan seterusnya.\n"
        "4. Dasarkan klaim teknis pada REFERENCE DOCS untuk produk yang diminta jika tersedia.\n"
        "5. Jangan mengarang spesifikasi. Jika detail tidak ada di dokumen referensi, tulis: Belum tersedia di basis pengetahuan.\n"
        "6. Jika tidak ada REFERENCE DOCS, tetap bantu membuat draf umum, tetapi jangan menyebutnya bersumber dari knowledge base.\n"
        "7. Jangan menulis pembuka meta seperti 'Berikut adalah'. Mulai langsung dari bagian 3.1.\n"
        "8. Output hanya isi Bab 3, tanpa penutup di luar dokumen."
    )


def _build_prompt(
    product: str,
    project_name: Optional[str],
    project_description: Optional[str],
    additional_context: Optional[str],
    knowledge_context: Optional[str],
) -> str:
    parts: list[str] = []

    if knowledge_context:
        parts.append(
            "=== REFERENCE DOCS ===\n"
            f"{knowledge_context}\n"
            "=== END ==="
        )
    else:
        parts.append(
            "=== REFERENCE DOCS ===\n"
            "Belum tersedia di basis pengetahuan untuk produk ini.\n"
            "=== END ==="
        )

    parts.append(f"Product: {product}")

    if project_name:
        parts.append(f"Project: {project_name}")
    if project_description:
        parts.append(f"Description: {project_description}")
    if additional_context:
        parts.append(f"Context:\n{additional_context}")

    parts.append(
        "Tulis Bab 3 lengkap untuk proposal RFP ini.\n"
        "WAJIB:\n"
        "- Mulai langsung dari 3.1, tanpa pengantar meta.\n"
        "- Gunakan Bahasa Indonesia.\n"
        "- Jelaskan detail produk, fitur utama, arsitektur/komponen, integrasi, keamanan, implementasi, dan manfaat.\n"
        "- Gunakan informasi dari REFERENCE DOCS bila tersedia.\n"
        "- Jika detail tidak tersedia di REFERENCE DOCS, tulis 'Belum tersedia di basis pengetahuan' pada bagian yang relevan.\n"
        "- Jangan menyebut bab 1, bab 2, bab 4, atau bab 5 kecuali hanya sebagai konteks singkat."
    )

    return "\n\n".join(parts)


async def _retrieve_knowledge_async(query: str, product: Optional[str]) -> tuple[str, list[str]]:
    """Run knowledge retrieval in a thread pool so it doesn't block the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _retrieve_knowledge_context(query, product=product),
    )


async def stream_technical_content(
    product: str,
    project_name: Optional[str] = None,
    project_description: Optional[str] = None,
    additional_context: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """
    Async generator yielding WebSocket-ready message dicts.

    Performance optimizations:
    - Token buffering (batches tokens into ~80-char chunks)
    - Persistent HTTP client (connection reuse)
    - Ollama inference tuning (num_ctx, num_batch)
    - Async knowledge retrieval (non-blocking)
    - Compact prompts (fewer input tokens)
    """
    system_prompt = _build_system_prompt()

    # Async knowledge retrieval — doesn't block event loop
    knowledge_query = f"Technical proposal for {product}"
    if project_description:
        knowledge_query += f". {project_description}"

    knowledge_context, sources = await _retrieve_knowledge_async(knowledge_query, product)

    if not sources:
        yield {
            "type": "warning",
            "message": f"Tidak ada dokumen pengetahuan untuk produk '{product}'. Hasil dapat tidak akurat.",
        }

    prompt = _build_prompt(
        product=product,
        project_name=project_name,
        project_description=project_description,
        additional_context=additional_context,
        knowledge_context=knowledge_context,
    )

    full_content = ""
    buffer = ""
    last_flush = time.monotonic()

    url = f"{OLLAMA_API}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": True,
        "options": {
            "temperature": 0.2,
            "num_predict": 8192,
            "num_ctx": 16384,
            "num_batch": 512,
            "num_thread": 8,
        },
    }

    try:
        client = await _get_client()
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                token = data.get("response", "")
                if token:
                    full_content += token
                    buffer += token

                    now = time.monotonic()
                    elapsed = now - last_flush

                    # Flush when buffer is large enough OR time interval passed
                    if len(buffer) >= CHUNK_SIZE or elapsed >= FLUSH_INTERVAL:
                        yield {
                            "type": "chunk",
                            "content": buffer,
                        }
                        buffer = ""
                        last_flush = now

                if data.get("done", False):
                    break

        # Flush remaining buffer
        if buffer:
            yield {
                "type": "chunk",
                "content": buffer,
            }

    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama returned HTTP {e.response.status_code}")
        yield {
            "type": "error",
            "message": f"LLM service returned status {e.response.status_code}",
        }
        return
    except httpx.ConnectError:
        logger.error("Cannot connect to Ollama service")
        yield {
            "type": "error",
            "message": "Cannot connect to the LLM service. Please ensure Ollama is running.",
        }
        return
    except Exception as e:
        logger.error(f"Unexpected error streaming technical content: {e}")
        yield {
            "type": "error",
            "message": f"Unexpected error: {str(e)}",
        }
        return

    yield {
        "type": "complete",
        "fullContent": full_content,
    }


def _build_adjust_system_prompt() -> str:
    return (
        "Anda adalah penulis teknis yang menyempurnakan Bab 3 RFP: Detail Produk. "
        "Gunakan Bahasa Indonesia, terapkan instruksi pengguna secara tepat, "
        "pertahankan bagian yang tidak perlu diubah, dan output hanya Bab 3 yang sudah direvisi. "
        "Jika detail tidak tersedia di dokumen referensi, tulis: Belum tersedia di basis pengetahuan."
    )


def _build_adjust_prompt(
    product: str,
    content: str,
    additional_context: Optional[str],
    knowledge_context: Optional[str],
) -> str:
    parts: list[str] = [
        f"Product: {product}",
        (
            "=== REFERENCE DOCS ===\n"
            f"{knowledge_context or 'Belum tersedia di basis pengetahuan untuk produk ini.'}\n"
            "=== END ==="
        ),
        f"=== CURRENT DRAFT ===\n{content}\n=== END DRAFT ===",
    ]

    if additional_context:
        parts.append(f"Adjustment instructions:\n{additional_context}")
    else:
        parts.append("Improve and refine this chapter. Fix any issues and enhance clarity.")

    parts.append("Output seluruh Bab 3 yang sudah direvisi dalam Bahasa Indonesia.")

    return "\n\n".join(parts)


async def stream_adjust_content(
    product: str,
    content: str,
    additional_context: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """
    Async generator that adjusts/refines existing RFP content.
    Streams the revised chapter token by token with buffering.
    """
    system_prompt = _build_adjust_system_prompt()
    knowledge_context, sources = await _retrieve_knowledge_async(
        f"Technical proposal refinement for {product}. {additional_context or ''}",
        product,
    )
    if not sources:
        yield {
            "type": "warning",
            "message": f"Tidak ada dokumen pengetahuan untuk produk '{product}'. Hasil dapat tidak akurat.",
        }
    prompt = _build_adjust_prompt(
        product=product,
        content=content,
        additional_context=additional_context,
        knowledge_context=knowledge_context,
    )

    full_content = ""
    buffer = ""
    last_flush = time.monotonic()

    url = f"{OLLAMA_API}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": True,
        "options": {
            "temperature": 0.3,
            "num_predict": 4096,
            "num_ctx": 4096,
            "num_batch": 512,
            "num_thread": 8,
        },
    }

    try:
        client = await _get_client()
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                token = data.get("response", "")
                if token:
                    full_content += token
                    buffer += token

                    now = time.monotonic()
                    elapsed = now - last_flush

                    if len(buffer) >= CHUNK_SIZE or elapsed >= FLUSH_INTERVAL:
                        yield {
                            "type": "chunk",
                            "content": buffer,
                        }
                        buffer = ""
                        last_flush = now

                if data.get("done", False):
                    break

        if buffer:
            yield {
                "type": "chunk",
                "content": buffer,
            }

    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama returned HTTP {e.response.status_code} during adjust")
        yield {
            "type": "error",
            "message": f"LLM service returned status {e.response.status_code}",
        }
        return
    except httpx.ConnectError:
        logger.error("Cannot connect to Ollama service")
        yield {
            "type": "error",
            "message": "Cannot connect to the LLM service. Please ensure Ollama is running.",
        }
        return
    except Exception as e:
        logger.error(f"Unexpected error during adjust: {e}")
        yield {
            "type": "error",
            "message": f"Unexpected error: {str(e)}",
        }
        return

    yield {
        "type": "complete",
        "fullContent": full_content,
    }

