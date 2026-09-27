"""Reads learner context from the LMS.

Every URL is built from LMS_BASE_URL, never taken from a webhook's `api_url`: the
request carries our LC-API-KEY, so it must only ever go to the configured LMS.
Redirects are not followed for the same reason.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from pydantic import ValidationError

from learner_memory.core.config import Settings
from learner_memory.integrations.lms.schemas import (
    PROFILE_INCLUDE,
    LmsContextEnvelope,
    LmsLearnerContext,
)

API_KEY_HEADER = "LC-API-KEY"
_CONTEXT_PATH = "api/learning-companion/v1/learners/{external_id}/context"


class LmsError(Exception):
    """The LMS could not be read or answered outside the contract. Reads are
    idempotent, so the caller may retry."""


class LmsClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        """`http` must already carry the base URL, LC-API-KEY header and timeout;
        `lms_client()` builds one from settings."""
        self._http = http

    async def fetch_learner_profile(self, external_id: int) -> LmsLearnerContext:
        """GET the learner's profile context.

        Raises LmsError on a transport failure or timeout, a non-200 status,
        `success: false`, or a body that does not match the contract.
        """
        url = _CONTEXT_PATH.format(external_id=external_id)
        try:
            resp = await self._http.get(url, params={"include": PROFILE_INCLUDE})
        except httpx.HTTPError as exc:
            raise LmsError(
                f"LMS request for learner {external_id} failed: {type(exc).__name__}"
            ) from exc
        if resp.status_code != httpx.codes.OK:
            raise LmsError(f"LMS returned HTTP {resp.status_code} for learner {external_id}")
        return _parse_context(resp.content, external_id)


def _parse_context(body: bytes, external_id: int) -> LmsLearnerContext:
    try:
        envelope = LmsContextEnvelope.model_validate_json(body)
    except ValidationError as exc:
        # Field locations only: the offending values may be personal data.
        locations = sorted(".".join(map(str, err["loc"])) for err in exc.errors())
        raise LmsError(
            f"LMS context for learner {external_id} breaks the contract at: {locations}"
        ) from None
    if not envelope.success or envelope.data is None:
        raise LmsError(f"LMS reported failure reading learner {external_id}")
    if envelope.data.user_id != external_id:
        raise LmsError(
            f"LMS answered for learner {envelope.data.user_id} when asked for {external_id}"
        )
    return envelope.data


@asynccontextmanager
async def lms_client(settings: Settings) -> AsyncIterator[LmsClient]:
    """An LmsClient whose connection pool is closed on exit.

    Raises RuntimeError if the LMS integration is not configured.
    """
    if not settings.lms_enabled or settings.lms_api_key is None:
        raise RuntimeError(
            "LMS integration is not configured: set LMS_BASE_URL, LMS_API_KEY "
            "and LMS_ORGANIZATION_ID"
        )
    async with httpx.AsyncClient(
        base_url=str(settings.lms_base_url),
        headers={API_KEY_HEADER: settings.lms_api_key.get_secret_value()},
        timeout=settings.lms_timeout_seconds,
        follow_redirects=False,
    ) as http:
        yield LmsClient(http)
