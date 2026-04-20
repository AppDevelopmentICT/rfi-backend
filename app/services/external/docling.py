import httpx
import logging
from app.config import DOCLING_API

logger = logging.getLogger(__name__)

async def parse_document(file_bytes: bytes, filename: str) -> str:
    endpoint = f"{DOCLING_API}/v1/convert/file"
    logger.info(f"Sending {filename} to Docling at {endpoint}")
    
    files = {
        'files': (filename, file_bytes, 'application/octet-stream')
    }
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(endpoint, files=files)
            if response.status_code != 200:
                logger.error(f"Docling Error: {response.text}")
                raise Exception(f"Docling returned status {response.status_code}")
            
            try:
                data = response.json()
                logger.info(f"Docling response keys: {list(data.keys())}")
                

                if "results" in data and isinstance(data["results"], list) and len(data["results"]) > 0:
                    texts = []
                    for result in data["results"]:
                        content = result.get("markdown") or result.get("text", "")
                        if content:
                            texts.append(content)
                    if texts:
                        return "\n\n".join(texts)
                
                content = data.get("markdown") or data.get("text")
                if content:
                    return content
                

                return str(data)
            except Exception as e:
                logger.warning(f"Failed to parse Docling JSON: {e}. Returning raw text.")
                return response.text
    except Exception as e:
        logger.error(f"Failed to communicate with Docling: {e}")
        raise e
