import logging
from typing import List, Dict, Any, Optional
from minio import Minio
from minio.error import S3Error
from app.config import MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET, MINIO_SECURE

logger = logging.getLogger(__name__)

_minio_client: Optional[Minio] = None


def get_minio_client() -> Minio:
    global _minio_client
    if _minio_client is None:
        _minio_client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
    return _minio_client


def list_bucket_objects(prefix: str = "") -> List[Dict[str, Any]]:
    """List all objects in the configured MinIO bucket."""
    client = get_minio_client()
    
    try:
        if not client.bucket_exists(MINIO_BUCKET):
            logger.warning(f"Bucket '{MINIO_BUCKET}' does not exist.")
            return []
        
        objects = []
        for obj in client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True):
            if obj.is_dir:
                continue
            objects.append({
                "key": obj.object_name,
                "size": obj.size,
                "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                "etag": obj.etag,
            })
        
        logger.info(f"Found {len(objects)} objects in bucket '{MINIO_BUCKET}'")
        return objects
    except S3Error as e:
        logger.error(f"MinIO S3Error listing bucket: {e}")
        raise
    except Exception as e:
        logger.error(f"Error listing MinIO bucket: {e}")
        raise


def download_object(key: str) -> bytes:
    """Download an object from MinIO and return its bytes."""
    client = get_minio_client()
    
    response = None
    try:
        response = client.get_object(MINIO_BUCKET, key)
        data = response.read()
        logger.info(f"Downloaded '{key}' ({len(data)} bytes) from MinIO")
        return data
    except S3Error as e:
        logger.error(f"MinIO S3Error downloading '{key}': {e}")
        raise
    except Exception as e:
        logger.error(f"Error downloading '{key}' from MinIO: {e}")
        raise
    finally:
        if response:
            response.close()
            response.release_conn()
