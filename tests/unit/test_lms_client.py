"""LMS client: what it requests, and every way a read can fail.

httpx's MockTransport stands in for the LMS, so no network is touched.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from learner_memory.core.config import Settings, get_settings
from learner_memory.integrations.lms.client import LmsClient, LmsError, lms_client

FIXTURE = Path(__file__).parent.parent / "fixtures" / "lms_learner_profile_context.json"
LMS_USER_ID = 90232436


def context_body() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def client_for(handler, base_url: str = "https://lms.test/") -> LmsClient:
    http = httpx.AsyncClient(
        base_url=base_url,
        headers={"LC-API-KEY": "lc-test-key"},
        transport=httpx.MockTransport(handler),
    )
    return LmsClient(http)


def responding(status_code: int, body) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)
    return handler


async def test_requests_profile_context_from_the_configured_lms():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=context_body())

    context = await client_for(handler).fetch_learner_profile(LMS_USER_ID)

    assert context.user_id == 90232436
    assert str(seen[0].url) == (
        "https://lms.test/api/learning-companion/v1/learners/90232436/context?include=profile"
    )
    assert seen[0].headers["LC-API-KEY"] == "lc-test-key"


async def test_base_url_path_prefix_is_kept():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=context_body())

    await client_for(handler, base_url="https://lms.test/prefix/").fetch_learner_profile(
        LMS_USER_ID
    )

    assert seen[0].url.path == "/prefix/api/learning-companion/v1/learners/90232436/context"


async def test_non_200_status_is_an_lms_error():
    with pytest.raises(LmsError, match="HTTP 503 for learner 90232436"):
        await client_for(responding(503, {})).fetch_learner_profile(LMS_USER_ID)


async def test_redirect_is_not_followed():
    """Following it would send the LC-API-KEY to wherever the redirect points."""
    def handler(_request):
        return httpx.Response(302, headers={"Location": "https://elsewhere.test/"})

    with pytest.raises(LmsError, match="HTTP 302"):
        await client_for(handler).fetch_learner_profile(LMS_USER_ID)


async def test_timeout_is_an_lms_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LmsError, match="failed: ReadTimeout"):
        await client_for(handler).fetch_learner_profile(LMS_USER_ID)


async def test_success_false_is_an_lms_error():
    body = {"success": False, "message": "Not found", "data": None}

    with pytest.raises(LmsError, match="reported failure"):
        await client_for(responding(200, body)).fetch_learner_profile(LMS_USER_ID)


async def test_missing_section_is_a_contract_error_naming_the_field():
    body = context_body()
    del body["data"]["profile"]["links"]

    with pytest.raises(LmsError, match=r"data\.profile\.links"):
        await client_for(responding(200, body)).fetch_learner_profile(LMS_USER_ID)


async def test_contract_error_does_not_echo_personal_data():
    body = context_body()
    body["data"]["profile"]["basic_info"]["email"] = "x" * 400 + "@example.com"

    with pytest.raises(LmsError) as exc:
        await client_for(responding(200, body)).fetch_learner_profile(LMS_USER_ID)

    assert "example.com" not in str(exc.value)
    assert "data.profile.basic_info.email" in str(exc.value)
    assert exc.value.__cause__ is None


async def test_answer_for_a_different_learner_is_rejected():
    body = context_body()
    body["data"]["user_id"] = 1

    with pytest.raises(LmsError, match="answered for learner 1 when asked for 90232436"):
        await client_for(responding(200, body)).fetch_learner_profile(LMS_USER_ID)


async def test_financial_fields_are_not_kept():
    context = await client_for(responding(200, context_body())).fetch_learner_profile(
        LMS_USER_ID
    )

    dumped = context.model_dump_json()
    assert "iban" not in dumped
    assert "EG380019000500000000263180002" not in dumped
    assert "bank_name" not in dumped


async def test_unconfigured_client_refuses_to_start():
    settings = get_settings().model_copy(update={"lms_base_url": None, "lms_api_key": None})

    with pytest.raises(RuntimeError, match="LMS integration is not configured"):
        async with lms_client(settings):
            pass


def test_partial_lms_configuration_fails_at_startup():
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(_env_file=None, lms_base_url=AnyHttpUrl("https://lms.test"))


def test_complete_lms_configuration_enables_the_integration():
    settings = Settings(
        _env_file=None,
        lms_base_url=AnyHttpUrl("https://lms.test"),
        lms_api_key=SecretStr("lc-test-key"),
        lms_organization_id="11111111-1111-1111-1111-111111111111",
    )

    assert settings.lms_enabled is True
