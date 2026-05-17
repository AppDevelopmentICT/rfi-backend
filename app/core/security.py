import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import API_AUTH_SECRET, POCKETBASE_URL, is_email_domain_allowed
from app.db.user_repo import upsert_user_from_pb_record

logger = logging.getLogger(__name__)

_token_cache: Dict[str, Tuple[dict, float]] = {}
_TOKEN_CACHE_TTL = 60
_TOKEN_CACHE_MAX = 500

REQUEST_LOGS: Dict[str, List[float]] = defaultdict(list)
RATE_LIMIT_STASH_TIME = 60
MAX_REQUESTS_PER_MINUTE = 300


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

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
    is_admin: bool
    is_service_account: bool
    token: Optional[str] = None


async def validate_pocketbase_token(token: str) -> Optional[dict]:
    """Returns PocketBase auth record dict or None. Results are cached for _TOKEN_CACHE_TTL seconds."""
    now = time.time()
    cached = _token_cache.get(token)
    if cached:
        record, expires_at = cached
        if now < expires_at:
            logger.debug("token cache hit")
            return record
        del _token_cache[token]
        logger.debug("token cache expired, evicting")

    if len(_token_cache) > _TOKEN_CACHE_MAX:
        expired_keys = [k for k, (_, exp) in _token_cache.items() if now >= exp]
        for k in expired_keys:
            del _token_cache[k]
        if len(_token_cache) > _TOKEN_CACHE_MAX:
            oldest_key = min(_token_cache, key=lambda k: _token_cache[k][1])
            del _token_cache[oldest_key]

    base = (POCKETBASE_URL or "").rstrip("/")
    if not base:
        return None
    url = f"{base}/api/collections/users/auth-refresh"
    headers_variants = (
        {"Authorization": f"Bearer {token}"},
        {"Authorization": token},
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
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
                _token_cache[token] = (record, now + _TOKEN_CACHE_TTL)
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
        is_admin=bool(user_row.is_admin),
        is_service_account=False,
        token=token,
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
            is_admin=True,
            is_service_account=True,
            token=token,
        )
    return await exchange_pocketbase_token(token)


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


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
