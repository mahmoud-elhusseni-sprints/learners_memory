"""LMS sync tasks: what the idempotency ledger records for each way a sync ends.

The ledger (Postgres) is replaced by an in-memory one and the LMS by httpx's
MockTransport; the task code and LmsClient run for real. The deletion tests also
replace the database session and learner repository; the deletion service runs
for real against that in-memory repository.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from learner_memory.db.models.learner import Learner, LearnerStatus
from learner_memory.integrations.lms.client import LmsClient
from learner_memory.workers.tasks import lms as tasks

EVENT_ID = uuid.UUID("0838311c-19de-42bb-b6c4-a2b39c9eebd1")
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 47185


class InMemoryLedger:
    def __init__(self):
        self.completed: list[tuple[str, str | None]] = []

    async def claim(self, task, key, args):
        return True

    async def complete(self, key, *, error=None):
        self.completed.append((key, error))


@pytest.fixture
def ledger(monkeypatch) -> InMemoryLedger:
    fake = InMemoryLedger()
    monkeypatch.setattr(tasks, "claim", fake.claim)
    monkeypatch.setattr(tasks, "complete", fake.complete)
    return fake


def lms_answering(status_code: int, body: dict):
    @asynccontextmanager
    async def fake_lms_client(_settings):
        transport = httpx.MockTransport(lambda _req: httpx.Response(status_code, json=body))
        async with httpx.AsyncClient(base_url="https://lms.test/", transport=transport) as http:
            yield LmsClient(http)
    return fake_lms_client


async def test_lms_404_is_recorded_as_not_found_and_not_retried(ledger, monkeypatch):
    monkeypatch.setattr(tasks, "lms_client", lms_answering(404, {"success": False}))

    result = await tasks._sync_learner_profile(EVENT_ID, LMS_USER_ID, ORG)

    assert result == {"outcome": "not_found"}
    assert ledger.completed == [(f"lms.sync_learner_profile:{EVENT_ID}", None)]


class DeletableLearners:
    """Stands in for LearnerRepository in the deletion task."""

    def __init__(self):
        self.learner = Learner(id=LEARNER, organization_id=ORG, external_id=LMS_USER_ID,
                               display_name="Ramiro Carter", status=LearnerStatus.ACTIVE)
        self.personal_reset = False

    async def lock_by_external_id(self, external_id):
        return self.learner if external_id == LMS_USER_ID else None

    async def reset_personal_data(self, _learner_id):
        self.personal_reset = True

    async def lock_profile(self, _learner_id):
        return None


@pytest.fixture
def learners(monkeypatch) -> DeletableLearners:
    fake = DeletableLearners()

    @asynccontextmanager
    async def no_database():
        yield None

    monkeypatch.setattr(tasks, "unit_of_work", no_database)
    monkeypatch.setattr(tasks, "LearnerRepository", lambda _session, _org: fake)
    return fake


async def test_deletion_confirmed_by_the_lms_soft_deletes_the_learner(
        ledger, learners, monkeypatch):
    monkeypatch.setattr(tasks, "lms_client", lms_answering(404, {"success": False}))

    result = await tasks._delete_learner(EVENT_ID, LMS_USER_ID, ORG)

    assert result == {"outcome": "applied"}
    assert learners.learner.status == "deleted"
    assert learners.personal_reset is True
    assert ledger.completed == [(f"lms.delete_learner:{EVENT_ID}", None)]


async def test_deletion_the_lms_does_not_confirm_changes_nothing_and_retries(
        ledger, learners, monkeypatch):
    """The wipe cannot be undone, so a user the LMS still holds is not deleted;
    the failure makes Celery retry in case the event outran the LMS commit."""
    monkeypatch.setattr(tasks, "lms_client", lms_answering(200, {"success": True}))

    with pytest.raises(tasks.LmsError, match="deletion not confirmed"):
        await tasks._delete_learner(EVENT_ID, LMS_USER_ID, ORG)

    assert learners.learner.status == "active"
    assert learners.personal_reset is False
    assert ledger.completed == [(f"lms.delete_learner:{EVENT_ID}",
                                 "LMS still holds learner 47185; deletion not confirmed")]


async def test_other_lms_failure_is_recorded_and_raised_for_retry(ledger, monkeypatch):
    monkeypatch.setattr(tasks, "lms_client", lms_answering(503, {}))

    with pytest.raises(tasks.LmsError):
        await tasks._sync_learner_profile(EVENT_ID, LMS_USER_ID, ORG)

    assert ledger.completed == [(f"lms.sync_learner_profile:{EVENT_ID}",
                                 "LMS returned HTTP 503 for learner 47185")]
