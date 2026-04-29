import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import API_AUTH_SECRET, POCKETBASE_URL, is_email_domain_allowed
from app.db.user_repo import upsert_user_from_pb_record

logger = logging.getLogger(__name__)

REQUEST_LOGS: Dict[str, List[float]] = defaultdict(list)
RATE_LIMIT_STASH_TIME = 60
MAX_REQUESTS_PER_MINUTE = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"

        now = time.time()

        REQUEST_LOGS[client_ip] = [
            t for t in REQUEST_LOGS[client_ip] if now - t < RATE_LIMIT_STASH_TIME
        ]

        if len(REQUEST_LOGS[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
            )

        REQUEST_LOGS[client_ip].append(now)
        response = await call_next(request)
        return response


security = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    id: Optional[int]
    pocketbase_id: Optional[str]
    email: Optional[str]
    name: Optional[str]
    is_service_account: bool


async def validate_pocketbase_token(token: str) -> Optional[dict]:
    """Returns PocketBase auth record dict or None."""
    base = (POCKETBASE_URL or "").rstrip("/")
    if not base:
        return None
    url = f"{base}/api/collections/users/auth-refresh"
    headers_variants = (
        {"Authorization": f"Bearer {token}"},
        {"Authorization": token},
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        for hdr in headers_variants:
            try:
                resp = await client.post(url, headers=hdr)
            except httpx.RequestError as e:
                logger.warning(
                    "Cannot reach PocketBase at %s (check POCKETBASE_URL and network): %s",
                    url,
                    e,
                )
                return None
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except ValueError:
                logger.warning("PocketBase auth-refresh returned non-JSON body")
                continue
            record = data.get("record") or data
            if isinstance(record, dict) and record.get("id"):
                return record
    return None


async def exchange_pocketbase_token(token: str) -> CurrentUser:
    record = await validate_pocketbase_token(token)
    if not record:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email_raw = (record.get("email") or "").strip()
    if not is_email_domain_allowed(email_raw):
        raise HTTPException(
            status_code=403,
            detail="Only company email addresses are allowed to use this application.",
        )
    user_row = upsert_user_from_pb_record(record)
    return CurrentUser(
        id=user_row.id,
        pocketbase_id=user_row.pocketbase_id,
        email=user_row.email,
        name=user_row.name or None,
        is_service_account=False,
    )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials.strip()
    if token == API_AUTH_SECRET:
        return CurrentUser(
            id=None,
            pocketbase_id=None,
            email=None,
            name=None,
            is_service_account=True,
        )
    return await exchange_pocketbase_token(token)


async def verify_bearer_any(token: Optional[str]) -> bool:
    if not token:
        return False
    if token == API_AUTH_SECRET:
        return True
    rec = await validate_pocketbase_token(token)
    if not rec:
        return False
    email_raw = (rec.get("email") or "").strip()
    return is_email_domain_allowed(email_raw)
