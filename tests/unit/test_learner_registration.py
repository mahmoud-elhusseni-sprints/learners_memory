"""Learner registration: the LMS `external_id` at the API boundary.

The repository is faked, so these cover validation, pass-through and error
mapping. The ON CONFLICT / unique-constraint behavior itself needs Postgres.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from learner_memory.api.deps import AuthContext, get_auth, learner_repo
from learner_memory.api.v1 import learners
from learner_memory.db.models.learner import Learner
from learner_memory.db.repositories.learner import ExternalIdTaken

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 90232436


class FakeLearnerRepo:
    """Records what register() received; optionally refuses the external id."""

    def __init__(self, *, taken_external_id: int | None = None):
        self.taken_external_id = taken_external_id
        self.calls: list[dict] = []

    async def register(self, **kwargs):
        self.calls.append(kwargs)
        external_id = kwargs["external_id"]
        if external_id is not None and external_id == self.taken_external_id:
            raise ExternalIdTaken(external_id)
        learner = Learner(
            id=kwargs["learner_id"],
            organization_id=ORG,
            external_id=external_id,
            display_name=kwargs["display_name"],
            status="active",
            program_id=kwargs["program_id"],
            cohort_id=kwargs["cohort_id"],
            registered_at=datetime(2026, 9, 28, 9, 0, tzinfo=UTC),
        )
        return learner, True


def build_client(repo: FakeLearnerRepo) -> TestClient:
    app = FastAPI()
    app.include_router(learners.router, prefix="/v1")
    app.dependency_overrides[get_auth] = lambda: AuthContext(
        ORG, "test", frozenset({"profile:write"})
    )
    app.dependency_overrides[learner_repo] = lambda: repo
    return TestClient(app)


def test_external_id_is_stored_and_returned():
    repo = FakeLearnerRepo()
    client = build_client(repo)

    resp = client.post("/v1/learners", json={"id": str(LEARNER), "external_id": LMS_USER_ID})

    assert resp.status_code == 201
    assert resp.json()["external_id"] == 90232436
    assert repo.calls[0]["external_id"] == 90232436


def test_registration_without_external_id_still_works():
    """Existing registration clients never send the field."""
    repo = FakeLearnerRepo()
    client = build_client(repo)

    resp = client.post("/v1/learners", json={"id": str(LEARNER), "display_name": "Ardith"})

    assert resp.status_code == 201
    assert resp.json()["external_id"] is None
    assert repo.calls[0]["external_id"] is None


def test_external_id_held_by_another_learner_is_a_conflict():
    client = build_client(FakeLearnerRepo(taken_external_id=LMS_USER_ID))

    resp = client.post("/v1/learners", json={"id": str(LEARNER), "external_id": LMS_USER_ID})

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "external_id 90232436 is already registered to another learner"
    )


def test_non_positive_external_id_is_rejected():
    repo = FakeLearnerRepo()
    client = build_client(repo)

    for bad in (0, -5):
        resp = client.post("/v1/learners", json={"id": str(LEARNER), "external_id": bad})
        assert resp.status_code == 422

    assert repo.calls == []


def test_external_id_beyond_bigint_is_rejected():
    repo = FakeLearnerRepo()
    client = build_client(repo)

    resp = client.post("/v1/learners",
                       json={"id": str(LEARNER), "external_id": 9223372036854775808})

    assert resp.status_code == 422
    assert repo.calls == []


def test_non_numeric_external_id_is_rejected():
    repo = FakeLearnerRepo()
    client = build_client(repo)

    resp = client.post("/v1/learners", json={"id": str(LEARNER), "external_id": "abc"})

    assert resp.status_code == 422
    assert repo.calls == []
