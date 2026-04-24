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
        "You are a technical writer for RFP proposals. "
        "Write Chapter 3: Technical Content. "
        "Rules: professional tone, specific to the product given, "
        "cover architecture/implementation/integration/security/scalability/maintenance, "
        "flowing prose (2-4 paragraphs per area), no preamble, no markdown headers, "
        "write as the vendor submitting the proposal."
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

    parts.append(f"Product: {product}")

    if project_name:
        parts.append(f"Project: {project_name}")
    if project_description:
        parts.append(f"Description: {project_description}")
    if additional_context:
        parts.append(f"Context:\n{additional_context}")

    parts.append(
        "Write the full Chapter 3: Technical Content for this RFP."
    )

    return "\n\n".join(parts)


async def _retrieve_knowledge_async(query: str) -> str:
    """Run knowledge retrieval in a thread pool so it doesn't block the event loop."""
    loop = asyncio.get_event_loop()
    context, _ = await loop.run_in_executor(None, _retrieve_knowledge_context, query)
    return context


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

    knowledge_context = await _retrieve_knowledge_async(knowledge_query)

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
            "temperature": 0.4,
            "num_predict": 4096,
            "num_ctx": 2048,       # smaller context window = faster inference
            "num_batch": 512,      # larger batch = faster prompt processing
            "num_thread": 8,       # use more CPU threads
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
        "You are a technical writer refining an RFP Chapter 3: Technical Content. "
        "The user has provided their current draft and adjustment instructions. "
        "Rules: keep the same professional tone, apply the requested changes precisely, "
        "preserve parts that don't need changing, improve clarity and flow, "
        "no preamble, no markdown headers, output only the revised full chapter."
    )


def _build_adjust_prompt(
    product: str,
    content: str,
    additional_context: Optional[str],
) -> str:
    parts: list[str] = [
        f"Product: {product}",
        f"=== CURRENT DRAFT ===\n{content}\n=== END DRAFT ===",
    ]

    if additional_context:
        parts.append(f"Adjustment instructions:\n{additional_context}")
    else:
        parts.append("Improve and refine this chapter. Fix any issues and enhance clarity.")

    parts.append("Output the full revised Chapter 3.")

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
    prompt = _build_adjust_prompt(
        product=product,
        content=content,
        additional_context=additional_context,
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

