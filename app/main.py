from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.excel_router import router as example_router
from app.routers.rfi_router import router as rfi_router

app = FastAPI(title="RFI/RFP Auto-Fill API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rfi_router)
app.include_router(example_router)