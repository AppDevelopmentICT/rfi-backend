import re
from typing import Optional
import httpx
from app.config import OLLAMA_API, OLLAMA_MODEL

# Words the LLM might use instead of a clean Yes/No
_YES_SYNONYMS = {
    "capable", "supported", "available", "compliant", "included",
    "provided", "enabled", "compatible", "implemented", "offered",
    "true", "correct", "affirmative", "confirmed",
}
_NO_SYNONYMS = {
    "incapable", "unsupported", "unavailable", "non-compliant",
    "not included", "not provided", "not available", "not supported",
    "disabled", "incompatible", "not implemented", "not offered",
    "false", "incorrect", "negative", "none",
}


def _normalize_boolean(text: str) -> str | None:
    """If text looks like a boolean/capability answer, normalize to Yes/No/N/A."""
    lower = text.lower().strip().rstrip(".,;:!").strip()

    # exact match
    if lower in ("yes", "no", "n/a"):
        return lower.capitalize() if lower != "n/a" else "N/A"

    # synonym match
    if lower in _YES_SYNONYMS:
        return "Yes"
    if lower in _NO_SYNONYMS:
        return "No"

    # starts with Yes/No + explanation (e.g. "Yes, it supports...")
    m = re.match(r"^(yes|no)\b[.,;:\-\u2014\u2013\s]", lower)
    if m:
        return m.group(1).capitalize()

    # "Capable/No" or "Yes/No" style
    m = re.match(r"^(\w+)\s*/\s*(\w+)$", lower)
    if m:
        w1, w2 = m.group(1).lower(), m.group(2).lower()
        if w1 in _YES_SYNONYMS or w1 == "yes":
            return "Yes"
        if w1 in _NO_SYNONYMS or w1 == "no":
            return "No"
        if w2 in _YES_SYNONYMS or w2 == "yes":
            return "Yes"
        if w2 in _NO_SYNONYMS or w2 == "no":
            return "No"

    return None  # not a boolean answer


def _clean_response(text: str) -> str:
    """Strip thinking tags, markdown formatting, and normalize output."""
    # Remove <think>...</think> blocks (qwen3 thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # Remove markdown bold markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)

    # Remove markdown bullet points
    text = re.sub(r"^\s*[-*\u2022]\s+", "", text, flags=re.MULTILINE)

    # Remove leading labels like "Why?", "Answer:", "Response:", etc.
    text = re.sub(
        r"^\s*(Why\??|Answer:?|Response:?|Note:?)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = text.strip()

    # Try to normalize to a single consistent word
    normalized = _normalize_boolean(text)
    if normalized is not None:
        return normalized

    # For multi-word answers, clean up leading "Yes—"/"No—" preamble
    text = re.sub(r"^\s*(Yes|No)\s*[\u2014\u2013\-:,]\s*", "", text, flags=re.IGNORECASE)

    # Strip conversational filler from the start of explanations
    filler_pattern = r"^\s*(Here is a simple explanation:?|Here is the answer:?|The explanation is:?|This means that|Basically,?|Simply put,?)\s*"
    text = re.sub(filler_pattern, "", text, flags=re.IGNORECASE)
    
    # Capitalize the first letter if it got stripped by the regex
    if text:
        text = text[0].upper() + text[1:]

    return text.strip()


async def ask_ollama(
    question: str,
    column_name: str,
    model: Optional[str] = None,
) -> str:
    """Send a question to the Ollama LLM and return the response.

    Args:
        question:    The RFI/RFP context text.
        column_name: The column header to fill.
        model:       Optional model override; defaults to OLLAMA_MODEL from config.
    """
    url = f"{OLLAMA_API}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": (
            f"Context:\n{question}\n\n"
            f"Column to fill: '{column_name}'\n\n"
            "Write ONLY the answer value for this cell."
        ),
        "system": (
            "You are filling spreadsheet cells for an RFI/RFP document.\n"
            "STRICT RULES:\n"
            "1. If the column expects a capability or yes/no answer, respond with EXACTLY ONE WORD: Yes, No, or N/A.\n"
            "2. Never write 'Capable', 'Supported', 'Available', or any synonym. Use ONLY 'Yes' or 'No'.\n"
            "3. Never combine words like 'Capable/No' or 'Yes/Supported'. Pick ONE word.\n"
            "4. For explanation or descriptive columns, give ONLY the core factual answer. Keep it very short and simple (1-2 sentences absolute maximum). Do not elaborate.\n"
            "5. Cut straight to the point. Never start with 'This is...', 'The explanation is...', or 'Here is...'.\n"
            "6. No markdown, no bold, no bullets, no labels, no preamble.\n"
            "7. Do not repeat the question or column name.\n"
            "8. Do not explain your reasoning.\n"
        ),
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            return _clean_response(raw)
    except Exception as e:
        return f"Error: {e}"
