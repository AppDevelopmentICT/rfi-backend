import os
from dotenv import load_dotenv

load_dotenv()

OLLAMA_API = os.getenv("OLLAMA_API", "http://10.0.80.13:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
API_AUTH_SECRET = os.getenv("API_AUTH_SECRET", "super-secret-default-key-change-me")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.join(BASE_DIR, "files")
SOURCE_FILE = os.path.join(FILES_DIR, "RFI-Examples.xlsx")
