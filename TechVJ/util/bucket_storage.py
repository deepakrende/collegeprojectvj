import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from info import DATABASE_NAME, OTHER_DB_URI, LOG_CHANNEL
from TechVJ.bot import TechVJBot
from TechVJ.util.file_properties import get_file_ids

logger = logging.getLogger(__name__)

BUCKET_NAME = os.environ.get("BUCKET", "").strip()
BUCKET_ENDPOINT = os.environ.get("ENDPOINT", "").strip()
BUCKET_ACCESS_KEY = os.environ.get("ACCESS_KEY_ID", "").strip()
BUCKET_SECRET_KEY = os.environ.get("SECRET_ACCESS_KEY", "").strip()
BUCKET_REGION = os.environ.get("REGION", "auto").strip() or "auto"

# Safety guard: by default, no more than 250 GB/month is uploaded from the
# Railway service into the bucket. At $0.05/GB service egress, this is $5.00
# of upload egress, leaving room for compute and other traffic under a $20 cap.
UPLOAD_LIMIT_GB = float(os.environ.get("BUCKET_MONTHLY_UPLOAD_GB_LIMIT", "100"))
UPLOAD_LIMIT_BYTES = int(UPLOAD_LIMIT_GB * 1024 * 1024 * 1024)

try:
    import pymongo
    _mongo = pymongo.MongoClient(OTHER_DB_URI)
    _db = _mongo[DATABASE_NAME]
    _cache_col = _db["RAILWAY_BUCKET_CACHE"]
    _budget_col = _db["RAILWAY_BUCKET_BUDGET"]
except Exception:
    _mongo = None
    _cache_col = None
    _budget_col = None

_locks = {}
_locks_guard = asyncio.Lock()
_cleanup_lock = asyncio.Lock()
_last_cleanup = 0.0

# Long-running media operations need explicit network/application timeouts.
# These are configurable because upload speed varies by Railway region/bucket.
TELEGRAM_DOWNLOAD_TIMEOUT = int(
    os.environ.get("BUCKET_TELEGRAM_DOWNLOAD_TIMEOUT", "900")
)
BUCKET_UPLOAD_TIMEOUT = int(
    os.environ.get("BUCKET_UPLOAD_TIMEOUT", "900")
)

# Multipart uploads are substantially more reliable for large video files.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=16 * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)


def bucket_enabled() -> bool:
    return bool(BUCKET_NAME and BUCKET_ENDPOINT and BUCKET_ACCESS_KEY and BUCKET_SECRET_KEY)


def _client():
    if not bucket_enabled():
        raise RuntimeError(
            "Railway Storage Bucket is not configured. Set BUCKET, ENDPOINT, "
            "ACCESS_KEY_ID and SECRET_ACCESS_KEY from the Railway Bucket variables."
        )
    return boto3.client(
        "s3",
        endpoint_url=BUCKET_ENDPOINT,
        aws_access_key_id=BUCKET_ACCESS_KEY,
        aws_secret_access_key=BUCKET_SECRET_KEY,
        region_name=BUCKET_REGION,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=30,
            read_timeout=120,
            tcp_keepalive=True,
        ),
    )


def _object_key(unique_id: str, file_name: str) -> str:
    # Telegram file_unique_id is stable for the same underlying media.
    safe_name = Path(file_name or "video.bin").name.replace("/", "_")
    return f"media/{unique_id}/{safe_name}"


def _head_object(key: str) -> bool:
    try:
        _client().head_object(Bucket=BUCKET_NAME, Key=key)
        return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _presign(key: str, expires: int) -> str:
    # The signed URL lifetime is bounded by the existing 24h link expiry.
    ttl = max(60, int(expires - time.time()))
    ttl = min(ttl, 7 * 24 * 3600)
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=ttl,
    )


def _period_key() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def _reserve_upload(size: int) -> bool:
    if _budget_col is None:
        # If Mongo is unavailable, fail closed rather than risking unbounded
        # Railway egress.
        return False

    period = _period_key()
    # Single-process lock prevents races in the normal Railway deployment.
    doc = _budget_col.find_one({"_id": period}) or {"bytes": 0}
    used = int(doc.get("bytes", 0))
    if used + size > UPLOAD_LIMIT_BYTES:
        return False
    _budget_col.update_one({"_id": period}, {"$set": {"bytes": used + size}}, upsert=True)
    return True


def _release_upload(size: int):
    if _budget_col is not None:
        _budget_col.update_one({"_id": _period_key()}, {"$inc": {"bytes": -int(size)}})


async def _lock_for(unique_id: str):
    async with _locks_guard:
        lock = _locks.get(unique_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[unique_id] = lock
        return lock


def _cleanup_old_objects(max_age_seconds: int = 7 * 24 * 3600):
    if _cache_col is None or not bucket_enabled():
        return
    cutoff = time.time() - max_age_seconds
    old = list(_cache_col.find({"updated_at": {"$lt": cutoff}}, {"_id": 1, "key": 1}))
    if not old:
        return
    client = _client()
    for doc in old:
        key = doc.get("key")
        if not key:
            continue
        try:
            client.delete_object(Bucket=BUCKET_NAME, Key=key)
        except Exception:
            logger.exception("Failed to remove old bucket object %s", key)
            continue
        _cache_col.delete_one({"_id": doc.get("_id")})


async def maybe_cleanup_old_objects():
    global _last_cleanup
    if not bucket_enabled():
        return
    now = time.time()
    if now - _last_cleanup < 6 * 3600:
        return
    async with _cleanup_lock:
        now = time.time()
        if now - _last_cleanup < 6 * 3600:
            return
        try:
            await asyncio.to_thread(_cleanup_old_objects)
            _last_cleanup = now
        except Exception:
            logger.exception("Bucket cleanup failed")


async def ensure_uploaded(file_id: int):
    """Ensure a Telegram media file exists in Railway Bucket.

    Returns (object_key, file_data). The first request downloads the file once
    to Railway ephemeral disk, uploads it to the bucket, then removes the temp
    file. Later requests never stream the file through Railway.
    """
    if not bucket_enabled():
        raise RuntimeError("Railway Storage Bucket is not configured.")

    await maybe_cleanup_old_objects()
    file_data = await get_file_ids(TechVJBot, int(LOG_CHANNEL), int(file_id))
    unique_id = str(file_data.unique_id)
    file_name = file_data.file_name or f"{unique_id}.bin"
    key = _object_key(unique_id, file_name)

    # Fast path: Mongo cache, then S3 HEAD as a repair path.
    if _cache_col is not None:
        cached = _cache_col.find_one({"_id": unique_id}, {"key": 1})
        if cached and cached.get("key") == key:
            try:
                exists = await asyncio.to_thread(_head_object, key)
                if exists:
                    return key, file_data
            except Exception:
                logger.exception("Bucket cache verification failed for %s", unique_id)

    lock = await _lock_for(unique_id)
    async with lock:
        if await asyncio.to_thread(_head_object, key):
            if _cache_col is not None:
                _cache_col.update_one(
                    {"_id": unique_id},
                    {"$set": {"key": key, "size": int(file_data.file_size or 0), "updated_at": time.time()}},
                    upsert=True,
                )
            return key, file_data

        size = int(file_data.file_size or 0)
        if size <= 0:
            raise RuntimeError("Telegram did not provide a valid file size.")

        # Hard monthly upload budget. We intentionally fail closed instead of
        # falling back to Telegram->Railway->viewer streaming.
        if not await asyncio.to_thread(_reserve_upload, size):
            raise RuntimeError(
                f"Monthly media migration safety limit reached ({UPLOAD_LIMIT_GB:g} GB). "
                "Video delivery is paused to protect the Railway budget."
            )

        tmp_path: Optional[str] = None
        reserved = True
        try:
            suffix = Path(file_name).suffix or ".bin"
            fd, tmp_path = tempfile.mkstemp(prefix="tcu_media_", suffix=suffix)
            os.close(fd)

            logger.info("Migrating Telegram media %s (%s bytes) to Railway Bucket", unique_id, size)
            message = await TechVJBot.get_messages(int(LOG_CHANNEL), int(file_id))
            if message.empty:
                raise RuntimeError("Source Telegram message was not found.")

            # Pyrofork writes the Telegram file directly to disk; Railway ingress
            # is not billed as network egress.
            logger.info(
                "Starting Telegram download: file=%s size=%s timeout=%ss",
                unique_id,
                size,
                TELEGRAM_DOWNLOAD_TIMEOUT,
            )
            try:
                downloaded = await asyncio.wait_for(
                    TechVJBot.download_media(message, file_name=tmp_path),
                    timeout=TELEGRAM_DOWNLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Telegram media download timed out after "
                    f"{TELEGRAM_DOWNLOAD_TIMEOUT}s."
                )

            if not downloaded or not os.path.exists(tmp_path):
                raise RuntimeError("Telegram media download failed.")

            logger.info(
                "Telegram download completed: file=%s actual_size=%s",
                unique_id,
                os.path.getsize(tmp_path),
            )

            actual_size = os.path.getsize(tmp_path)
            if actual_size != size:
                logger.warning("Telegram size mismatch: metadata=%s actual=%s", size, actual_size)

            logger.info(
                "Starting bucket upload: file=%s key=%s size=%s timeout=%ss",
                unique_id,
                key,
                actual_size,
                BUCKET_UPLOAD_TIMEOUT,
            )

            def _upload():
                client = _client()
                client.upload_file(
                    tmp_path,
                    BUCKET_NAME,
                    key,
                    ExtraArgs={
                        "ContentType": file_data.mime_type or "application/octet-stream",
                        "ContentDisposition": f'inline; filename="{Path(file_name).name}"',
                        "CacheControl": "private, max-age=0",
                    },
                    Config=TRANSFER_CONFIG,
                )

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_upload),
                    timeout=BUCKET_UPLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Bucket upload timed out after {BUCKET_UPLOAD_TIMEOUT}s."
                )

            # Verify the object before telling the browser that it is ready.
            logger.info("Verifying bucket object: %s", key)
            if not await asyncio.to_thread(_head_object, key):
                raise RuntimeError(
                    f"Bucket upload completed but object was not found: {key}"
                )

            logger.info("Bucket upload completed successfully: %s", key)

            if _cache_col is not None:
                _cache_col.update_one(
                    {"_id": unique_id},
                    {"$set": {
                        "key": key,
                        "size": size,
                        "file_name": file_name,
                        "mime_type": file_data.mime_type or "application/octet-stream",
                        "updated_at": time.time(),
                    }},
                    upsert=True,
                )
            reserved = False
            return key, file_data
        except Exception:
            logger.exception("Media migration failed: file=%s key=%s", unique_id, key)
            if reserved:
                await asyncio.to_thread(_release_upload, size)
            raise
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


async def get_presigned_url(file_id: int, expires: int) -> str:
    key, _ = await ensure_uploaded(file_id)
    return await asyncio.to_thread(_presign, key, int(expires))
