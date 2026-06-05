import asyncio
import functools

import boto3
from botocore.client import BaseClient

from nodalpulse.settings import settings

_client: BaseClient | None = None


def get_client() -> BaseClient:
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )
    return _client


def upload(key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
    get_client().put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


async def upload_async(key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, functools.partial(upload, key, body, content_type))


def download(key: str) -> bytes:
    resp = get_client().get_object(Bucket=settings.r2_bucket, Key=key)
    return resp["Body"].read()


def exists(key: str) -> bool:
    try:
        get_client().head_object(Bucket=settings.r2_bucket, Key=key)
        return True
    except Exception:
        return False
