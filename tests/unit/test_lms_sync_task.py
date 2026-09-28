"""The LMS sync registry, and the one task that runs every sync.

The ledger, the LMS client and the unit of work are replaced with fakes, so
nothing leaves the process.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from learner_memory.integrations.lms.client import LmsError
from learner_memory.integrations.lms.schemas import (
    LmsContext,
    LmsJourneyContext,
    LmsProfileContext,
)
from learner_memory.integrations.lms.syncs import LmsSync, LmsSyncKind, sync_for, sync_of
from learner_memory.workers.tasks import lms as lms_tasks

EVENT_ID = uuid.UUID("4ecb2672-127d-49ac-8923-529c24b71ef0")
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LMS_USER_ID = 90232436
SESSION = object()
CONTEXT = LmsContext(user_id=LMS_USER_ID, last_updated_at=None)


def test_profile_include_picks_the_profile_sync():
    sync = sync_for(frozenset({"profile"}))

    assert sync is sync_of("profile")
    assert sync.kind is LmsSyncKind.PROFILE
    assert sync.context_model is LmsProfileContext
    assert sync.scope_param is None


def test_enrollments_and_progress_include_picks_the_journey_sync():
    sync = sync_for(frozenset({"progress", "enrollments"}))

    assert sync is sync_of("journey")
    assert sync.context_model is LmsJourneyContext
    assert sync.scope_param == "journey_id"


@pytest.mark.parametrize("includes", [
    frozenset(),
    frozenset({"certificates"}),
    frozenset({"enrollments"}),
    frozenset({"profile", "enrollments", "progress"}),
])
def test_includes_without_a_sync_pick_nothing(includes):
    assert sync_for(includes) is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        sync_of("payments")


def a_sync(apply=None, scope_param=None) -> LmsSync:
    async def applied(*_args):
        return "applied"

    return LmsSync(kind=LmsSyncKind.PROFILE, includes=("enrollments", "progress"),
                   context_model=LmsContext, apply=apply or applied, scope_param=scope_param)


def test_unscoped_sync_reads_without_scope():
    assert a_sync().scope(None) is None


def test_scoped_sync_reads_its_scope():
    assert a_sync(scope_param="journey_id").scope(1654) == {"journey_id": 1654}


def test_scoped_sync_refuses_to_read_without_its_scope():
    with pytest.raises(ValueError, match="needs journey_id"):
        a_sync(scope_param="journey_id").scope(None)


class Ledger:
    def __init__(self, already_done: bool = False):
        self.already_done = already_done
        self.claimed: list[tuple[str, str, dict]] = []
        self.completed: list[tuple[str, str | None]] = []

    async def claim(self, task, key, args):
        self.claimed.append((task, key, args))
        return not self.already_done

    async def complete(self, key, *, error=None):
        self.completed.append((key, error))


class FakeLms:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.reads: list[tuple] = []

    async def fetch_context(self, external_id, model, includes, scope):
        self.reads.append((external_id, model, includes, scope))
        if self.error:
            raise self.error
        return CONTEXT


@pytest.fixture
def ledger(monkeypatch) -> Ledger:
    ledger = Ledger()
    monkeypatch.setattr(lms_tasks, "claim", ledger.claim)
    monkeypatch.setattr(lms_tasks, "complete", ledger.complete)
    return ledger


@pytest.fixture
def lms(monkeypatch) -> FakeLms:
    lms = FakeLms()

    @asynccontextmanager
    async def fake_client(_settings):
        yield lms

    @asynccontextmanager
    async def fake_unit_of_work():
        yield SESSION

    monkeypatch.setattr(lms_tasks, "lms_client", fake_client)
    monkeypatch.setattr(lms_tasks, "unit_of_work", fake_unit_of_work)
    return lms


async def test_sync_reads_what_it_names_and_applies_it(ledger, lms):
    applied: list[tuple] = []

    async def apply(*args):
        applied.append(args)
        return "applied"

    result = await lms_tasks._sync_learner(
        EVENT_ID, LMS_USER_ID, ORG, a_sync(apply, scope_param="journey_id"), 1654
    )

    assert result == {"outcome": "applied"}
    assert lms.reads == [(LMS_USER_ID, LmsContext, ("enrollments", "progress"),
                          {"journey_id": 1654})]
    assert applied == [(SESSION, ORG, LMS_USER_ID, 1654, CONTEXT)]
    key = f"lms.sync_learner:{EVENT_ID}"
    assert ledger.claimed[0][:2] == ("lms.sync_learner", key)
    assert ledger.completed == [(key, None)]


async def test_already_synced_event_is_skipped(ledger, lms):
    ledger.already_done = True

    result = await lms_tasks._sync_learner(EVENT_ID, LMS_USER_ID, ORG, a_sync(), None)

    assert result == {"skipped": True}
    assert lms.reads == []
    assert ledger.completed == []


async def test_failed_read_is_recorded_and_raised_for_retry(ledger, lms):
    lms.error = LmsError("LMS returned HTTP 503 for learner 90232436")

    with pytest.raises(LmsError):
        await lms_tasks._sync_learner(EVENT_ID, LMS_USER_ID, ORG, a_sync(), None)

    assert ledger.completed == [(f"lms.sync_learner:{EVENT_ID}",
                                 "LMS returned HTTP 503 for learner 90232436")]


async def test_failed_apply_records_only_the_error_type(ledger, lms):
    async def apply(*_args):
        raise RuntimeError("duplicate key value (email)=(someone@example.com)")

    with pytest.raises(RuntimeError):
        await lms_tasks._sync_learner(EVENT_ID, LMS_USER_ID, ORG, a_sync(apply), None)

    assert ledger.completed == [(f"lms.sync_learner:{EVENT_ID}", "RuntimeError")]
