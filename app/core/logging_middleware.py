import time
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger("uvicorn.error")

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        
        
        method = request.method
        url = request.url.path
        client_ip = request.client.host if request.client else "unknown"
        
        try:
            response = await call_next(request)
            process_time = time.perf_counter() - start_time
            
            
            logger.info(
                f"{client_ip} - \"{method} {url}\" {response.status_code} "
                f"({process_time:.4f}s)"
            )
            return response
            
        except Exception as e:
            process_time = time.perf_counter() - start_time
            logger.error(
                f"{client_ip} - \"{method} {url}\" ERROR: {str(e)} "
                f"({process_time:.4f}s)"
            )
            raise e
