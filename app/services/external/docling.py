import httpx
import logging
from app.config import (
    DOCLING_API,
    DOCLING_OCR_LANG,
    DOCLING_TABLE_MODE,
    DOCLING_DOCUMENT_TIMEOUT,
    DOCLING_ABORT_ON_ERROR,
)

logger = logging.getLogger(__name__)

async def parse_document(file_bytes: bytes, filename: str) -> str:
    endpoint = f"{DOCLING_API}/v1/convert/file"
    logger.info(f"Sending {filename} to Docling at {endpoint}")
    
    files = {
        'files': (filename, file_bytes, 'application/octet-stream')
    }

    data = {
        'to_formats': ['md'],
        'image_export_mode': 'embedded',
        'pipeline_type': 'standard',
        'do_ocr': 'true',
        'ocr_engine': 'tesseract',
        'ocr_lang': DOCLING_OCR_LANG,
        'do_table_structure': 'true',
        'table_mode': DOCLING_TABLE_MODE,
        'abort_on_error': str(DOCLING_ABORT_ON_ERROR).lower(),
    }
    
    try:
        async with httpx.AsyncClient(timeout=DOCLING_DOCUMENT_TIMEOUT) as client:
            response = await client.post(endpoint, files=files, data=data)
            if response.status_code != 200:
                logger.error(f"Docling Error: {response.text}")
                raise Exception(f"Docling returned status {response.status_code}")
            
            try:
                result = response.json()

                doc = result.get("document", {})
                md = doc.get("md_content")
                if md:
                    return md

                if "results" in result and isinstance(result["results"], list):
                    texts = []
                    for r in result["results"]:
                        content = r.get("markdown") or r.get("text", "")
                        if content:
                            texts.append(content)
                    if texts:
                        return "\n\n".join(texts)

                content = result.get("markdown") or result.get("text")
                if content:
                    return content

                return str(result)
            except Exception as e:
                logger.warning(f"Failed to parse Docling JSON: {e}. Returning raw text.")
                return response.text
    except Exception as e:
        logger.error(f"Failed to communicate with Docling: {e}")
        raise e
