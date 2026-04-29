import logging

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException



from app.routers.excel_router import router as example_router
from app.routers.rfi_router import router as rfi_router
from app.routers.document_router import router as document_router
from app.routers.ai_router import router as ai_router
from app.routers.knowledge_router import router as knowledge_router
from app.routers.rfp_router import router as rfp_router
from app.core.security import RateLimitMiddleware
from app.core.logging_middleware import LoggingMiddleware
from app.config import ALLOWED_ORIGINS
from contextlib import asynccontextmanager
from app.db.database import init_db

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):

    init_db()
    yield

app = FastAPI(
    title="RFI/RFP Auto-Fill API", 
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LoggingMiddleware)
app.add_middleware(RateLimitMiddleware)

@app.exception_handler(HTTPException)
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": f"HTTP_{exc.status_code}", "message": exc.detail}},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "details": exc.errors()}},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected error occurred"}},
    )

app.include_router(rfi_router)
app.include_router(example_router)
app.include_router(document_router)
app.include_router(ai_router)
app.include_router(knowledge_router)
app.include_router(rfp_router)