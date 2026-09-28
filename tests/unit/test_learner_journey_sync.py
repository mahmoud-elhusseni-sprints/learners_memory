"""Mirroring LMS journey enrollment onto the learner, from the LMS response on.

An in-memory repository holds a real ORM object, so the assertions read the same
attribute the database would persist. Row locking needs Postgres and is not
covered here.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from learner_memory.db.models.learner import Learner
from learner_memory.integrations.lms.mapping import is_enrolled
from learner_memory.integrations.lms.schemas import LmsContextEnvelope, LmsJourneyContext
from learner_memory.services.learner_journey_sync import LearnerJourneySync
from learner_memory.services.learner_profile_sync import SyncOutcome

FIXTURE = Path(__file__).parent.parent / "fixtures" / "lms_learner_journey_context.json"
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 90232436
JOURNEY = 1654


def context(*enrollment_overrides: dict) -> LmsJourneyContext:
    """The captured response; each override dict replaces the enrollment's fields,
    and passing none keeps the captured enrollment."""
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    captured = body["data"]["enrollments"][0]
    if enrollment_overrides:
        body["data"]["enrollments"] = [{**captured, **o} for o in enrollment_overrides]
    return LmsContextEnvelope[LmsJourneyContext].model_validate(body).data


def test_captured_response_parses_without_progress():
    parsed = context()

    assert [e.journey_id for e in parsed.enrollments] == [JOURNEY]
    assert "progress" not in parsed.model_dump()


def test_active_enrollment_is_enrolled():
    assert is_enrolled(context(), JOURNEY) is True


def test_blocked_enrollment_is_not_enrolled():
    assert is_enrolled(context({"blocked": True}), JOURNEY) is False


def test_no_enrollment_for_the_journey_is_not_enrolled():
    assert is_enrolled(context({"journey_id": 99}), JOURNEY) is False


def test_empty_enrollments_is_not_enrolled():
    parsed = context()
    parsed.enrollments = []

    assert is_enrolled(parsed, JOURNEY) is False


class InMemoryLearners:
    def __init__(self, learner: Learner | None):
        self.learner = learner

    async def lock_by_external_id(self, external_id):
        if self.learner is not None and self.learner.external_id == external_id:
            return self.learner
        return None


def registered(*journey_ids: int) -> InMemoryLearners:
    return InMemoryLearners(Learner(id=LEARNER, organization_id=ORG, external_id=LMS_USER_ID,
                                    external_journey_ids=list(journey_ids)))


async def test_enrollment_adds_the_journey_in_order():
    repo = registered(2000, 12)

    outcome = await LearnerJourneySync(repo).apply(LMS_USER_ID, JOURNEY, enrolled=True)

    assert outcome is SyncOutcome.APPLIED
    assert repo.learner.external_journey_ids == [12, JOURNEY, 2000]


async def test_leaving_removes_only_that_journey():
    repo = registered(12, JOURNEY, 2000)

    outcome = await LearnerJourneySync(repo).apply(LMS_USER_ID, JOURNEY, enrolled=False)

    assert outcome is SyncOutcome.APPLIED
    assert repo.learner.external_journey_ids == [12, 2000]


@pytest.mark.parametrize(("journey_ids", "enrolled"), [
    ((JOURNEY,), True),
    ((), False),
    ((12,), False),
])
async def test_already_matching_enrollment_is_unchanged(journey_ids, enrolled):
    repo = registered(*journey_ids)

    outcome = await LearnerJourneySync(repo).apply(LMS_USER_ID, JOURNEY, enrolled=enrolled)

    assert outcome is SyncOutcome.UNCHANGED
    assert repo.learner.external_journey_ids == list(journey_ids)


async def test_learner_without_journeys_yet_gets_the_first():
    """A learner object built before the column existed has no list yet."""
    repo = registered()
    repo.learner.external_journey_ids = None

    await LearnerJourneySync(repo).apply(LMS_USER_ID, JOURNEY, enrolled=True)

    assert repo.learner.external_journey_ids == [JOURNEY]


async def test_unregistered_external_id_changes_nothing():
    repo = registered()

    outcome = await LearnerJourneySync(repo).apply(1, JOURNEY, enrolled=True)

    assert outcome is SyncOutcome.UNKNOWN_LEARNER
    assert repo.learner.external_journey_ids == []
