"""Credential verification: learn-os JWTs, and API keys for remote services."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import HTTPException, status
from jose import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.core.config import Settings
from learner_memory.db.models.api_key import ApiKey

_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0.0}
_JWKS_TTL = 3600


async def _jwks(settings: Settings) -> dict:
    if _jwks_cache["keys"] and time.time() - _jwks_cache["fetched_at"] < _JWKS_TTL:
        return _jwks_cache["keys"]
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(str(settings.jwt_jwks_url))
        resp.raise_for_status()
    _jwks_cache.update(keys=resp.json(), fetched_at=time.time())
    return _jwks_cache["keys"]


async def verify_token(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(
            token,
            await _jwks(settings),
            algorithms=["RS256", "ES256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc


# --------------------------------------------------------------------------- #
# API keys for remote services
# --------------------------------------------------------------------------- #
KEY_SCHEME = "lm"
_PREFIX_BYTES = 6
_SECRET_BYTES = 32


def hash_api_key(raw: str) -> str:
    """Plain SHA-256 is right here, unlike for passwords: the secret carries 256
    bits of entropy, so there is nothing to brute-force and verification stays on
    the hot read path."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, prefix, key_hash). The raw key is shown exactly once."""
    prefix = secrets.token_hex(_PREFIX_BYTES)
    raw = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(_SECRET_BYTES)}"
    return raw, prefix, hash_api_key(raw)


def parse_prefix(raw: str) -> str | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_SCHEME or not parts[1] or not parts[2]:
        return None
    return parts[1]


async def verify_api_key(raw: str, session: AsyncSession) -> ApiKey:
    """Look the key up by its prefix, then compare hashes in constant time.

    Every failure mode answers with the same 401 message: a caller must not be
    able to tell 'no such key' from 'wrong secret' from 'revoked'.
    """
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    prefix = parse_prefix(raw)
    if prefix is None:
        raise unauthorized

    key = (
        await session.execute(
            select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.active.is_(True))
        )
    ).scalar_one_or_none()

    # Hash unconditionally so a miss and a wrong secret cost the same.
    digest = hash_api_key(raw)
    if key is None or not hmac.compare_digest(key.key_hash, digest):
        raise unauthorized
    if key.revoked_at is not None:
        raise unauthorized
    if key.expires_at is not None and key.expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "api key expired")
    return key


async def touch_api_key(key_id: uuid.UUID) -> None:
    """Record usage in its own transaction — never inside the request's.

    Best effort by design: the response has already been sent, so a failure here
    must stay a log line rather than surface as an error on a successful read.
    """
    from learner_memory.core.logging import get_logger
    from learner_memory.db.session import unit_of_work

    try:
        async with unit_of_work() as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=datetime.now(UTC))
            )
    except Exception as exc:  # noqa: BLE001 - usage telemetry is never worth a 500
        get_logger(__name__).warning("apikey.touch_failed", key_id=str(key_id), error=str(exc))
