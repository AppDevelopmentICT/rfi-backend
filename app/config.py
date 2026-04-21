import os
from dotenv import load_dotenv


load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))


OLLAMA_API = os.getenv("OLLAMA_API", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")


DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://user:password@localhost:5432/dbname"
)


LANGCHAIN_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")


DOCLING_API = os.getenv("DOCLING_API", "http://localhost:5001")
DOCLING_OCR_LANG = [lang.strip() for lang in os.getenv("DOCLING_OCR_LANG", "en,id").split(",") if lang.strip()]
DOCLING_INCLUDE_IMAGES = os.getenv("DOCLING_INCLUDE_IMAGES", "false").lower() == "true"
DOCLING_TABLE_MODE = os.getenv("DOCLING_TABLE_MODE", "fast")
DOCLING_DOCUMENT_TIMEOUT = float(os.getenv("DOCLING_DOCUMENT_TIMEOUT", "180"))
DOCLING_ABORT_ON_ERROR = os.getenv("DOCLING_ABORT_ON_ERROR", "true").lower() == "true"


MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "rfi")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"


ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
API_AUTH_SECRET = os.getenv("API_AUTH_SECRET", "change-me-in-production")


os.environ["OLLAMA_BASE_URL"] = OLLAMA_API
os.environ["OLLAMA_HOST"] = OLLAMA_API


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.join(BASE_DIR, "files")
SOURCE_FILE = os.path.join(FILES_DIR, "RFI-Examples.xlsx")


os.makedirs(FILES_DIR, exist_ok=True)
