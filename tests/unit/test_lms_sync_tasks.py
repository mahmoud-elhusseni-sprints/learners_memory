"""LMS sync tasks: what the idempotency ledger records for each way a sync ends.

The ledger (Postgres) is replaced by an in-memory one and the LMS by httpx's
MockTransport; the task code and LmsClient run for real.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from learner_memory.integrations.lms.client import LmsClient
from learner_memory.workers.tasks import lms as tasks

EVENT_ID = uuid.UUID("0838311c-19de-42bb-b6c4-a2b39c9eebd1")
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
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


async def test_other_lms_failure_is_recorded_and_raised_for_retry(ledger, monkeypatch):
    monkeypatch.setattr(tasks, "lms_client", lms_answering(503, {}))

    with pytest.raises(tasks.LmsError):
        await tasks._sync_learner_profile(EVENT_ID, LMS_USER_ID, ORG)

    assert ledger.completed == [(f"lms.sync_learner_profile:{EVENT_ID}",
                                 "LMS returned HTTP 503 for learner 47185")]
