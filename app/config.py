import os
from typing import Optional

from dotenv import load_dotenv


load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))


OLLAMA_API = os.getenv("OLLAMA_API", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")


DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://user:password@localhost:5432/dbname"
)

# Seconds. Prevents indefinite hang during startup when PostgreSQL is unreachable (VPN off, wrong host).
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "15"))


LANGCHAIN_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")


# Docling — https://docling-project.github.io/docling/
# Typical setup: docling-serve (HTTP). Optional: pip install docling for in-process parsing.

def _sanitize_docling_api_base(url: str) -> str:
    """Strip accidental ``…/v1`` or ``…/v1alpha`` suffixes so routes are not doubled."""
    u = url.rstrip().rstrip("/")
    lower = u.lower()
    for suf in ("/v1", "/v1alpha"):
        while lower.endswith(suf):
            u = u[: -len(suf)].rstrip("/")
            lower = u.lower()
    return u


_raw_docling_api = os.getenv("DOCLING_API", "http://localhost:5001").strip()
DOCLING_API = _sanitize_docling_api_base(_raw_docling_api)
DOCLING_API_KEY = os.getenv("DOCLING_API_KEY", "").strip()
# Optional extra path before /v1/... (reverse proxy mount), e.g. "api" → http://host:5001/api/v1/convert/file
DOCLING_SERVE_PATH_PREFIX = os.getenv("DOCLING_SERVE_PATH_PREFIX", "").strip().strip("/")
# Comma-separated path segments tried after DOCLING_API (discovery), e.g. "infer,documents/api"
_extra_docling_path_prefix = os.getenv("DOCLING_SERVE_EXTRA_PATH_PREFIXES", "").strip()
DOCLING_SERVE_EXTRA_PATH_PREFIXES: tuple[str, ...] = tuple(
    seg.strip().strip("/") for seg in _extra_docling_path_prefix.split(",") if seg.strip()
)
# Older docling-serve containers (v0.x) only expose /v1alpha/...; stable v1 uses /v1/...
# Values: auto (v1 then v1alpha), v1 (v1 only), v1alpha (v1alpha then v1 fallback).
DOCLING_SERVE_API_SEGMENT = os.getenv("DOCLING_SERVE_API_SEGMENT", "auto").strip().lower()
# remote: HTTP-only (docling-serve).
# embedded: Python docling library only (see requirements-docling-embedded.txt).
# embedded_then_remote: try embedded first; if unavailable or weak, use HTTP.
DOCLING_MODE = os.getenv("DOCLING_MODE", "remote").strip().lower()
# auto (default): POST /v1/convert/source, then multipart /v1/convert/file if 404/405/422.
# source_json: same fallbacks as auto (JSON first). multipart: file upload only (no JSON source).
DOCLING_REMOTE_TRANSPORT = os.getenv("DOCLING_REMOTE_TRANSPORT", "auto").strip().lower()

DOCLING_OCR_LANG = [lang.strip() for lang in os.getenv("DOCLING_OCR_LANG", "en,id").split(",") if lang.strip()]
DOCLING_TABLE_MODE = os.getenv("DOCLING_TABLE_MODE", "accurate")
DOCLING_DOCUMENT_TIMEOUT = float(os.getenv("DOCLING_DOCUMENT_TIMEOUT", "180"))
# Seconds between GET /status/poll/{task_id} when using async convert fallback.
DOCLING_ASYNC_POLL_INTERVAL = float(os.getenv("DOCLING_ASYNC_POLL_INTERVAL", "2"))
# After async jobs fail or time out once, rerun with lighter Docling options (tables off, abort_on_error off).
DOCLING_ASYNC_LITE_FALLBACK = os.getenv("DOCLING_ASYNC_LITE_FALLBACK", "true").lower() == "true"
DOCLING_ABORT_ON_ERROR = os.getenv("DOCLING_ABORT_ON_ERROR", "true").lower() == "true"
DOCLING_PDF_BACKEND = os.getenv("DOCLING_PDF_BACKEND", "docling_parse").strip().lower()
# Match docling-serve docs: pdf backend + OCR stack (easyocr aligns with bundled defaults).
DOCLING_OCR_ENGINE = os.getenv("DOCLING_OCR_ENGINE", "easyocr").strip().lower()
# docling-core / docling-serve v1 use ``ocr_preset`` (replaces deprecated ``ocr_engine`` in JSON options).
DOCLING_OCR_PRESET = os.getenv("DOCLING_OCR_PRESET", DOCLING_OCR_ENGINE).strip().lower()
# Forces OCR text even when PDF has a glyph layer — fixes “looks like text but unselectable / broken vectors”.
DOCLING_FORCE_OCR = os.getenv("DOCLING_FORCE_OCR", "false").lower() == "true"
# If True, run a second Docling pass with force_ocr when the first result looks empty or noise-like.
DOCLING_AUTO_RETRY_FORCE_OCR = os.getenv("DOCLING_AUTO_RETRY_FORCE_OCR", "true").lower() == "true"

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "rfi")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"


# CORS: comma-separated origins. Local Next.js dev origins are merged in so
# localhost:3000 works even when ALLOWED_ORIGINS lists only production URLs.
_CANONICAL_LOCAL_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
_origins_raw = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000")
_parsed_origins = [o.strip() for o in _origins_raw.split(",") if o.strip()]
if not _parsed_origins:
    _parsed_origins = list(_CANONICAL_LOCAL_ORIGINS)

_seen_origins: set[str] = set()
ALLOWED_ORIGINS: list[str] = []
for _o in _parsed_origins + list(_CANONICAL_LOCAL_ORIGINS):
    if _o not in _seen_origins:
        _seen_origins.add(_o)
        ALLOWED_ORIGINS.append(_o)
API_AUTH_SECRET = os.getenv("API_AUTH_SECRET", "change-me-in-production")


POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")

# Comma-separated domains (e.g. infracom-tech.com). Empty or * = no restriction.
_ALLOWED_EMAIL_DOMAINS_RAW = os.getenv("ALLOWED_EMAIL_DOMAINS", "infracom-tech.com").strip()
if not _ALLOWED_EMAIL_DOMAINS_RAW or _ALLOWED_EMAIL_DOMAINS_RAW == "*":
    ALLOWED_EMAIL_DOMAIN_SET: Optional[frozenset] = None
else:
    ALLOWED_EMAIL_DOMAIN_SET = frozenset(
        d.strip().lower()
        for d in _ALLOWED_EMAIL_DOMAINS_RAW.split(",")
        if d.strip()
    )


def is_email_domain_allowed(email: Optional[str]) -> bool:
    if ALLOWED_EMAIL_DOMAIN_SET is None:
        return True
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain in ALLOWED_EMAIL_DOMAIN_SET


os.environ["OLLAMA_BASE_URL"] = OLLAMA_API
os.environ["OLLAMA_HOST"] = OLLAMA_API


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.join(BASE_DIR, "files")
SOURCE_FILE = os.path.join(FILES_DIR, "RFI-Examples.xlsx")


os.makedirs(FILES_DIR, exist_ok=True)
