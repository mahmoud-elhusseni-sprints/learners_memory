"""JWT verification against the learn-os issuer's JWKS."""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import HTTPException, status
from jose import jwt

from learner_memory.core.config import Settings

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
