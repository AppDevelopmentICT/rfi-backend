import re
import logging
from typing import Optional
import httpx
from app.config import OLLAMA_API, OLLAMA_MODEL

logger = logging.getLogger(__name__)


_YES_SYNONYMS = {
    "capable", "supported", "available", "compliant", "included",
    "provided", "enabled", "compatible", "implemented", "offered",
    "true", "correct", "affirmative", "confirmed", "absolutely",
    "certainly", "indeed", "definitely", "of course", "can",
    "possible", "feasible", "achievable", "doable",
}
_NO_SYNONYMS = {
    "incapable", "unsupported", "unavailable", "non-compliant",
    "not included", "not provided", "not available", "not supported",
    "disabled", "incompatible", "not implemented", "not offered",
    "false", "incorrect", "negative", "none", "cannot", "can't",
    "impossible", "infeasible", "not possible", "not feasible",
    "not capable", "no support", "not compatible",
}

# Column name patterns that indicate a Yes/No answer is expected
_BOOLEAN_COLUMN_PATTERNS = [
    r"yes\s*/?\s*no",
    r"y\s*/?\s*n",
    r"capability",
    r"complian",
    r"support(?:ed)?",
    r"available",
    r"enabled",
]


def _is_boolean_column(column_name: str) -> bool:
    """Determine if a column header expects a Yes/No answer."""
    lower = column_name.lower().strip()
    for pattern in _BOOLEAN_COLUMN_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def _normalize_boolean(text: str) -> str | None:
    """If text looks like a boolean/capability answer, normalize to Yes/No/N/A."""
    lower = text.lower().strip().rstrip(".,;:!").strip()

    if lower in ("yes", "no", "n/a", "na", "not applicable"):
        if lower in ("n/a", "na", "not applicable"):
            return "N/A"
        return lower.capitalize()

    if lower in _YES_SYNONYMS:
        return "Yes"
    if lower in _NO_SYNONYMS:
        return "No"

    # Match "yes" or "no" at start followed by punctuation/space
    m = re.match(r"^(yes|no)\b[.,;:\-\u2014\u2013\s]", lower)
    if m:
        return m.group(1).capitalize()

    # Match word/word patterns like "Capable/No"
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

    return None


def _force_boolean(text: str) -> str:
    """Force a response into Yes/No/N/A when the column demands it.

    This is more aggressive than _normalize_boolean — it scans the full
    text for positive/negative signals when a direct match isn't found.
    """
    # First try the standard normalization
    normalized = _normalize_boolean(text)
    if normalized is not None:
        return normalized

    lower = text.lower().strip()

    # Check if the first word/sentence starts with yes or no
    first_line = lower.split("\n")[0].strip()
    m = re.match(r"^(yes|no)\b", first_line)
    if m:
        return m.group(1).capitalize()

    # Scan for strong negative signals first (order matters)
    negative_phrases = [
        r"\bcannot\b", r"\bcan't\b", r"\bcan not\b",
        r"\bnot\s+(?:possible|supported|available|capable|feasible|compatible|able)\b",
        r"\bdoes\s+not\b", r"\bdoesn't\b", r"\bdo\s+not\b",
        r"\bunable\b", r"\bno\s+support\b", r"\bnot\s+natively\b",
        r"\bnot\s+directly\b",
    ]
    for pattern in negative_phrases:
        if re.search(pattern, lower):
            return "No"

    # Scan for strong positive signals
    positive_phrases = [
        r"\byes\b", r"\bcan\b", r"\bsupports?\b", r"\bcapable\b",
        r"\bavailable\b", r"\bprovides?\b", r"\boffers?\b",
        r"\benables?\b", r"\ballows?\b", r"\bcompatible\b",
        r"\bnatively\b", r"\bout[- ]of[- ]the[- ]box\b",
    ]
    positive_count = sum(1 for p in positive_phrases if re.search(p, lower))

    negative_count = sum(1 for p in negative_phrases if re.search(p, lower))

    if positive_count > negative_count:
        return "Yes"
    if negative_count > 0:
        return "No"

    # If we really can't tell, default to N/A
    if not lower or lower in ("unknown", "unclear", "uncertain"):
        return "N/A"

    # Last resort: if the text is short and looks affirmative
    if len(lower.split()) <= 5:
        if any(w in lower for w in ("yes", "can", "support", "available")):
            return "Yes"
        if any(w in lower for w in ("no", "not", "cannot", "can't")):
            return "No"

    return "Yes" if positive_count > 0 else "N/A"


def _clean_response(text: str, force_boolean: bool = False) -> str:
    """Strip thinking tags, markdown formatting, and normalize output.

    Args:
        text:          Raw LLM output.
        force_boolean: If True, the response MUST be Yes/No/N/A.
    """
    # Remove <think> tags
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # Remove markdown bold
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)

    # Remove bullet points
    text = re.sub(r"^\s*[-*\u2022]\s+", "", text, flags=re.MULTILINE)

    # Remove common preamble labels
    text = re.sub(
        r"^\s*(Why\??|Answer:?|Response:?|Note:?)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = text.strip()

    # For boolean columns, aggressively normalize
    if force_boolean:
        return _force_boolean(text)

    # For non-boolean columns, still try standard normalization
    normalized = _normalize_boolean(text)
    if normalized is not None:
        return normalized

    # Strip leading "Yes/No —" from descriptive answers
    text = re.sub(r"^\s*(Yes|No)\s*[\u2014\u2013\-:,]\s*", "", text, flags=re.IGNORECASE)

    # Remove filler openings
    filler_pattern = r"^\s*(Here is a simple explanation:?|Here is the answer:?|The explanation is:?|This means that|Basically,?|Simply put,?)\s*"
    text = re.sub(filler_pattern, "", text, flags=re.IGNORECASE)

    # Capitalize first letter
    if text:
        text = text[0].upper() + text[1:]

    return text.strip()


def _retrieve_knowledge_context(question: str, top_k: int = 5) -> tuple[str, list[str]]:
    """Retrieve relevant chunks from the PGVector knowledge base.

    Returns a tuple of (context_string, source_filenames).
    source_filenames contains unique document source names from metadata.
    If no results or failure, returns ("", []).
    """
    try:
        from app.services.knowledge.ingestion import _get_vector_store
        vector_store = _get_vector_store()
        results = vector_store.similarity_search(question, k=top_k)

        if not results:
            return "", []

        context_parts = []
        for i, doc in enumerate(results, 1):
            source = doc.metadata.get("source", "Unknown")
            context_parts.append(
                f"[Document: {source}]\n{doc.page_content.strip()}"
            )

        sources = list(dict.fromkeys(
            doc.metadata.get("source", "Unknown") for doc in results
        ))

        return "\n\n---\n\n".join(context_parts), sources

    except Exception as e:
        logger.warning(f"Knowledge base retrieval failed: {e}")
        return "", []


async def ask_ollama(
    question: str,
    column_name: str,
    model: Optional[str] = None,
    knowledge_context: Optional[str] = None,
) -> str:
    """Send a question to the Ollama LLM and return the response.

    Args:
        question:          The RFI/RFP context text.
        column_name:       The column header to fill.
        model:             Optional model override; defaults to OLLAMA_MODEL from config.
        knowledge_context: Optional pre-retrieved knowledge base context.
                           If None, the function will attempt to retrieve it automatically.
    """
    # Determine if this column expects a boolean answer
    boolean_column = _is_boolean_column(column_name)

    # Retrieve knowledge base context if not provided
    if knowledge_context is None:
        knowledge_context, _ = _retrieve_knowledge_context(question)

    # Build the prompt with knowledge context
    prompt_parts = []

    if knowledge_context:
        prompt_parts.append(
            "=== REFERENCE DOCUMENTS (from knowledge base) ===\n"
            f"{knowledge_context}\n"
            "=== END OF REFERENCE DOCUMENTS ==="
        )

    prompt_parts.append(f"Question:\n{question}")
    prompt_parts.append(f"Column to fill: '{column_name}'")

    if boolean_column:
        prompt_parts.append(
            "RESPOND WITH EXACTLY ONE WORD: Yes, No, or N/A. Nothing else."
        )
    else:
        prompt_parts.append("Write ONLY the answer value for this cell.")

    prompt = "\n\n".join(prompt_parts)

    # Build system prompt based on column type
    if boolean_column:
        system_prompt = (
            "You are filling a Yes/No column in an RFI/RFP spreadsheet.\n"
            "ABSOLUTE RULES:\n"
            "1. Your ENTIRE response must be exactly ONE word: Yes, No, or N/A.\n"
            "2. NEVER write anything other than Yes, No, or N/A.\n"
            "3. No explanations, no sentences, no punctuation, no qualifiers.\n"
            "4. If reference documents are provided, base your answer ONLY on the information in those documents.\n"
            "5. If no reference documents are provided or they don't contain relevant information, answer based on your general knowledge.\n"
            "6. 'Yes' means the capability/feature exists or is supported.\n"
            "7. 'No' means the capability/feature does NOT exist or is NOT supported.\n"
            "8. 'N/A' means the question is not applicable.\n"
        )
    else:
        system_prompt = (
            "You are filling spreadsheet cells for an RFI/RFP document.\n"
            "STRICT RULES:\n"
            "1. If reference documents are provided, base your answer ONLY on the information in those documents.\n"
            "2. If no reference documents are provided or they don't contain relevant information, answer based on your general knowledge.\n"
            "3. Give ONLY the core factual answer. Keep it very short and simple (1-2 sentences absolute maximum). Do not elaborate.\n"
            "4. Cut straight to the point. Never start with 'This is...', 'The explanation is...', or 'Here is...'.\n"
            "5. No markdown, no bold, no bullets, no labels, no preamble.\n"
            "6. Do not repeat the question or column name.\n"
            "7. Do not explain your reasoning.\n"
            "8. Do not start with Yes or No for descriptive/explanation columns.\n"
        )

    url = f"{OLLAMA_API}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {
            "temperature": 0.05 if boolean_column else 0.1,
            "num_predict": 10 if boolean_column else 256,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            return _clean_response(raw, force_boolean=boolean_column)
    except Exception as e:
        return f"Error: {e}"
