"""Supabase Storage adapter — a single private bucket for all raw evidence."""
from __future__ import annotations

import asyncio
from functools import lru_cache

from supabase import Client, create_client

from learner_memory.core.config import Settings, get_settings
from learner_memory.core.logging import get_logger

log = get_logger(__name__)


class SupabaseStorage:
    """Implements `ObjectStorage`. The SDK is sync, so calls are offloaded."""

    def __init__(self, settings: Settings | None = None, client: Client | None = None) -> None:
        self._s = settings or get_settings()
        self._client = client or create_client(self._s.supabase_url, self._s.supabase_service_key)
        self._bucket_name = self._s.storage_bucket

    @property
    def _bucket(self):
        return self._client.storage.from_(self._bucket_name)

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        await asyncio.to_thread(
            self._bucket.upload,
            key,
            data,
            {"content-type": content_type or "application/octet-stream", "upsert": "true"},
        )
        log.info("storage.put", key=key, bytes=len(data))
        return key

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._bucket.download, key)

    async def signed_url(self, key: str, *, expires_in: int = 300) -> str:
        res = await asyncio.to_thread(self._bucket.create_signed_url, key, expires_in)
        return res["signedURL"]

    async def signed_upload_url(self, key: str, *, expires_in: int = 3600) -> str:
        res = await asyncio.to_thread(self._bucket.create_signed_upload_url, key)
        return res["signed_url"]

    async def exists(self, key: str) -> bool:
        try:
            await self.get(key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._bucket.remove, [key])

    def ensure_bucket(self) -> None:
        """Idempotent bootstrap; called once at startup."""
        existing = {b.name for b in self._client.storage.list_buckets()}
        if self._bucket_name not in existing:
            self._client.storage.create_bucket(self._bucket_name, options={"public": False})
            log.info("storage.bucket_created", bucket=self._bucket_name)


@lru_cache
def get_storage() -> SupabaseStorage:
    return SupabaseStorage()
