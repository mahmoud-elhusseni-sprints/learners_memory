"""POST /v1/webhooks/lms/learner: authentication, validation, routing.

The session and repository are stubbed and Celery's send_task is recorded, so
nothing leaves the process.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, SecretStr

from learner_memory.api.v1 import webhooks
from learner_memory.core.config import get_settings
from learner_memory.db.models.learner import Learner
from learner_memory.db.session import get_session

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 90232436
EVENT_ID = "4ecb2672-127d-49ac-8923-529c24b71ef0"
API_KEY = "lc-test-key"
URL = "/v1/webhooks/lms/learner"


class FakeLearners:
    def __init__(self, *registered_external_ids: int):
        self.registered = registered_external_ids

    async def get_by_external_id(self, external_id):
        if external_id not in self.registered:
            return None
        return Learner(id=LEARNER, organization_id=ORG, external_id=external_id)


@pytest.fixture
def queued(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(webhooks.celery_app, "send_task",
                        lambda name, kwargs: sent.append((name, kwargs)))
    return sent


def build_client(learners: FakeLearners, *, lms_enabled: bool = True) -> TestClient:
    lms = {"lms_base_url": AnyHttpUrl("https://lms.test"), "lms_api_key": SecretStr(API_KEY),
           "lms_organization_id": ORG}
    if not lms_enabled:
        lms = {"lms_base_url": None, "lms_api_key": None, "lms_organization_id": None}
    settings = get_settings().model_copy(update=lms)

    async def _session():
        yield None

    app = FastAPI()
    app.include_router(webhooks.router, prefix="/v1")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[webhooks.lms_learner_repo] = lambda: learners
    return TestClient(app)


def an_event(include: str = "profile", **overrides) -> dict:
    """The "User updated" webhook body captured from the LMS."""
    body = {
        "event": "content.updated",
        "domain": "http://127.0.0.1:8000/",
        "api_url": "http://127.0.0.1:8000/api/learning-companion/v1/learners/90232436/"
                   f"context?include={include}",
        "user_id": LMS_USER_ID,
        "event_id": EVENT_ID,
        "entity_id": LMS_USER_ID,
        "entity_type": "learner",
        "occurred_at": "2026-09-16T05:18:05+03:00",
    }
    body.update(overrides)
    return body


def headers(api_key: str = API_KEY, event_id: str = EVENT_ID) -> dict:
    return {"LC-API-KEY": api_key, "X-Learning-Companion-Event-Id": event_id}


def test_profile_event_for_a_registered_learner_is_queued(queued):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json=an_event(), headers=headers())

    assert resp.status_code == 202
    assert resp.json() == {"event_id": EVENT_ID, "status": "queued"}
    assert queued == [("lms.sync_learner_profile", {
        "event_id": EVENT_ID,
        "external_id": 90232436,
        "organization_id": "11111111-1111-1111-1111-111111111111",
    })]


@pytest.mark.parametrize("event_name", ["content.created", "content.updated"])
def test_journey_event_is_queued_with_its_journey(queued, event_name):
    """JourneyLearner created and cached journey progress changed share one shape."""
    client = build_client(FakeLearners(LMS_USER_ID))
    event = an_event(include="enrollments%2Cprogress&journey_id=1654", journey_id=1654,
                     event=event_name)

    resp = client.post(URL, json=event, headers=headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert queued == [("lms.sync_learner_journey", {
        "event_id": EVENT_ID,
        "external_id": 90232436,
        "organization_id": "11111111-1111-1111-1111-111111111111",
        "journey_id": 1654,
    })]


def test_journey_event_without_journey_id_is_rejected(queued):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json=an_event(include="enrollments%2Cprogress"), headers=headers())

    assert resp.status_code == 422
    assert queued == []


def test_api_url_host_never_reaches_the_sync(queued):
    """The sync builds its own URL from LMS_BASE_URL; a hostile api_url is inert."""
    client = build_client(FakeLearners(LMS_USER_ID))
    event = an_event(api_url="https://attacker.test/steal?include=profile")

    resp = client.post(URL, json=event, headers=headers())

    assert resp.status_code == 202
    assert "attacker.test" not in str(queued)


def test_missing_api_key_is_unauthorized(queued):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json=an_event(), headers={"X-Learning-Companion-Event-Id": EVENT_ID})

    assert resp.status_code == 401
    assert queued == []


def test_wrong_api_key_is_unauthorized(queued):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json=an_event(), headers=headers(api_key="lc-wrong-key"))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid LC-API-KEY"
    assert queued == []


def test_unauthenticated_request_learns_nothing_about_the_body(queued):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json={"nonsense": True}, headers=headers(api_key="lc-wrong-key"))

    assert resp.status_code == 401


def test_unconfigured_integration_is_unavailable(queued):
    client = build_client(FakeLearners(LMS_USER_ID), lms_enabled=False)

    resp = client.post(URL, json=an_event(), headers=headers())

    assert resp.status_code == 503
    assert queued == []


def test_unregistered_learner_is_reported_not_registered(queued):
    client = build_client(FakeLearners())

    resp = client.post(URL, json=an_event(), headers=headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "learner with external_id 90232436 is not registered yet"
    assert queued == []


def test_event_for_data_not_synced_yet_is_ignored(queued):
    client = build_client(FakeLearners(LMS_USER_ID))
    event = an_event(include="forms&form_id=44844", form_id=44844, event="content.created")

    resp = client.post(URL, json=event, headers=headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert queued == []


def test_mismatched_event_id_header_is_rejected(queued):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json=an_event(),
                       headers=headers(event_id="3c400232-32c7-4e99-bf0c-b53e054dcf01"))

    assert resp.status_code == 422
    assert queued == []


@pytest.mark.parametrize("overrides", [
    {"entity_type": "journey"},
    {"event": "content.deleted"},
    {"user_id": 0},
    {"user_id": "abc"},
    {"event_id": "not-a-uuid"},
    {"occurred_at": "2026-09-16T05:18:05"},
    {"api_url": "not a url"},
])
def test_malformed_envelope_is_rejected(queued, overrides):
    client = build_client(FakeLearners(LMS_USER_ID))

    resp = client.post(URL, json=an_event(**overrides), headers=headers())

    assert resp.status_code == 422
    assert queued == []
