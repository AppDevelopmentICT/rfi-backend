import time
from collections import defaultdict
from typing import Dict, List

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import API_AUTH_SECRET


REQUEST_LOGS: Dict[str, List[float]] = defaultdict(list)
RATE_LIMIT_STASH_TIME = 60 # 1 minute
MAX_REQUESTS_PER_MINUTE = 60

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host
        now = time.time()
        
        REQUEST_LOGS[client_ip] = [t for t in REQUEST_LOGS[client_ip] if now - t < RATE_LIMIT_STASH_TIME]
        
        if len(REQUEST_LOGS[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."}
            )
        
        REQUEST_LOGS[client_ip].append(now)
        response = await call_next(request)
        return response


security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_AUTH_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"user": "authorized_app"}
