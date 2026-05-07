import logging

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr

from app.config import POCKETBASE_URL, is_email_domain_allowed
from app.core.security import CurrentUser, get_current_user
from app.db.user_repo import upsert_user_from_pb_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


# ── Request / Response schemas ────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    token: str
    record: dict


class ErrorResponse(BaseModel):
    error: dict


# ── POST /login ───────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
        403: {"model": ErrorResponse, "description": "Email domain not allowed"},
        502: {"model": ErrorResponse, "description": "PocketBase unreachable"},
    },
)
async def login(body: LoginRequest):
    """
    Authenticate a user via PocketBase's ``authWithPassword`` endpoint.

    On success the response contains ``token`` and ``record`` so the
    frontend can call ``pb.authStore.save(token, record)`` to hydrate
    the client-side session.
    """
    base_url = (POCKETBASE_URL or "").rstrip("/")
    if not base_url:
        logger.error("POCKETBASE_URL is not configured")
        return _error(502, "AUTH_SERVICE_UNAVAILABLE", "Authentication service is not configured")

    pb_auth_url = f"{base_url}/api/collections/users/auth-with-password"

    # ── 1. Call PocketBase authWithPassword ────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                pb_auth_url,
                json={"identity": body.email, "password": body.password},
            )
    except httpx.RequestError as exc:
        logger.error("Cannot reach PocketBase at %s: %s", pb_auth_url, exc)
        return _error(502, "AUTH_SERVICE_UNAVAILABLE", "Authentication service is currently unavailable")

    # ── 2. Handle PocketBase error responses ──────────────────────
    if resp.status_code != 200:
        # PocketBase returns 400 for bad credentials
        if resp.status_code in (400, 401, 403):
            return _error(401, "INVALID_CREDENTIALS", "Invalid email or password")
        logger.warning("PocketBase unexpected status %s: %s", resp.status_code, resp.text[:300])
        return _error(502, "AUTH_SERVICE_ERROR", "Authentication service returned an unexpected error")

    # ── 3. Parse success response ─────────────────────────────────
    try:
        data = resp.json()
    except ValueError:
        logger.error("PocketBase returned non-JSON body")
        return _error(502, "AUTH_SERVICE_ERROR", "Authentication service returned an invalid response")

    token: str = data.get("token", "")
    record: dict = data.get("record", {})

    if not token or not record.get("id"):
        logger.error("PocketBase response missing token or record id")
        return _error(502, "AUTH_SERVICE_ERROR", "Authentication service returned an incomplete response")

    # ── 4. Enforce email-domain policy ────────────────────────────
    email_raw = (record.get("email") or "").strip()
    if not is_email_domain_allowed(email_raw):
        return _error(403, "DOMAIN_NOT_ALLOWED", "Only company email addresses are allowed to use this application")

    # ── 5. Upsert local user row ──────────────────────────────────
    try:
        upsert_user_from_pb_record(record)
    except Exception:
        # Non-fatal: the login should still succeed even if the local
        # DB upsert fails – the token is already valid.
        logger.exception("Failed to upsert local user record for %s", email_raw)

    # ── 6. Return token + record for pb.authStore.save() ──────────
    return {"token": token, "record": record}


# ── GET /me (existing) ────────────────────────────────────────────────

@router.get("/me")
async def get_me(user: CurrentUser = Depends(get_current_user)):
    return {
        "id": user.id,
        "pocketbase_id": user.pocketbase_id,
        "email": user.email,
        "name": user.name,
        "is_admin": user.is_admin,
        "is_service_account": user.is_service_account,
    }


# ── Helpers ───────────────────────────────────────────────────────────

def _error(status: int, code: str, message: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )
