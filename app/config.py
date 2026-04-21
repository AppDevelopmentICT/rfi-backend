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


ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
API_AUTH_SECRET = os.getenv("API_AUTH_SECRET", "change-me-in-production")


os.environ["OLLAMA_BASE_URL"] = OLLAMA_API
os.environ["OLLAMA_HOST"] = OLLAMA_API


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.join(BASE_DIR, "files")
SOURCE_FILE = os.path.join(FILES_DIR, "RFI-Examples.xlsx")


os.makedirs(FILES_DIR, exist_ok=True)
