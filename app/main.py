from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.excel_router import router as example_router
from app.routers.rfi_router import router as rfi_router
from app.routers.document_router import router as document_router
from app.routers.ai_router import router as ai_router
from app.core.security import RateLimitMiddleware
from app.config import ALLOWED_ORIGINS

app = FastAPI(title="RFI/RFP Auto-Fill API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

app.include_router(rfi_router)
app.include_router(example_router)
app.include_router(document_router)
app.include_router(ai_router)