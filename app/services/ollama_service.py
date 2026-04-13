import re
from typing import Optional
import httpx
from app.config import OLLAMA_API, OLLAMA_MODEL


def _clean_response(text: str) -> str:
    """Strip thinking tags, markdown formatting, and preamble from LLM output."""
    # Remove <think>...</think> blocks (qwen3 thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # Remove markdown bold markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)

    # Remove leading labels like "Why?", "Answer:", "Response:", etc.
    text = re.sub(
        r"^\s*(Why\??|Answer:?|Response:?|Note:?)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove leading "Yes—" / "No—" style preamble (keep the substance)
    text = re.sub(r"^\s*(Yes|No)\s*[—–\-:]\s*", "", text, flags=re.IGNORECASE)

    return text.strip()


async def ask_ollama(
    question: str,
    column_name: str,
    model: Optional[str] = None,
) -> str:
    """Send a question to the Ollama LLM and return the response.

    Args:
        question:    The RFI/RFP question text.
        column_name: The column header to fill.
        model:       Optional model override; defaults to OLLAMA_MODEL from config.
    """
    url = f"{OLLAMA_API}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": (
            f"RFI/RFP Question: {question}\n"
            f"Column to fill: '{column_name}'\n"
            "Provide ONLY the answer. No labels, no preamble, no markdown."
        ),
        "system": (
            "You are a technology consultant filling in RFI/RFP spreadsheet cells. "
            "Rules:\n"
            "- Write a direct, factual answer suitable for a spreadsheet cell.\n"
            "- Do NOT start with labels like 'Why?', 'Answer:', or 'Yes—'.\n"
            "- Do NOT use markdown formatting (no bold, no bullets, no headers).\n"
            "- Do NOT repeat the question or the column name.\n"
            "- Keep the answer concise (1-3 sentences).\n"
        ),
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 256},
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            return _clean_response(raw)
    except Exception as e:
        return f"Error: {e}"

