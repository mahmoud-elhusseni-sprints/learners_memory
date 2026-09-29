"""Applying LMS journey enrollment and progress to a learner's learning journey.

In-memory repositories hold real ORM objects, so the assertions read the same
attributes the database would persist. Row locking and ON CONFLICT need Postgres
and are not covered here.
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from learner_memory.db.models.learner import (
    JourneyStep,
    Learner,
    LearnerProfile,
    LearningJourney,
)
from learner_memory.services.external_sync import SyncOutcome
from learner_memory.services.learner_journey_sync import (
    EnrollmentUpdate,
    JourneyUpdate,
    LearnerJourneySync,
    ProgramCounters,
    ProgramProgress,
    ProgressUpdate,
)

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 90232436
JOURNEY_ID = 1654
PROGRAM_ID = 2001668
ENROLLED_AT = datetime(2026, 9, 16, 2, 18, 5, tzinfo=UTC)
PROGRESS_AT = datetime(2026, 9, 16, 2, 18, 8, tzinfo=UTC)


class InMemoryLearners:
    def __init__(self, profile: LearnerProfile | None):
        self.learner = Learner(id=LEARNER, organization_id=ORG, external_id=LMS_USER_ID)
        self.profile = profile

    async def get_by_external_id(self, external_id):
        return self.learner if external_id == LMS_USER_ID else None

    async def lock_profile(self, learner_id):
        return self.profile if learner_id == LEARNER else None


class InMemoryJourneys:
    def __init__(self):
        self.journeys: list[LearningJourney] = []
        self.steps: list[JourneyStep] = []

    async def create_if_missing(self, learner_id, external_id, status):
        if await self.lock_by_external_id(learner_id, external_id) is None:
            self.journeys.append(LearningJourney(
                id=uuid.uuid4(), learner_id=learner_id, external_id=external_id, status=status,
                name=None, progress=Decimal(0), blocked=False, manual_added=False, plan={},
                enrollment_updated_at=None, progress_updated_at=None,
            ))

    async def lock_by_external_id(self, learner_id, external_id):
        return next((j for j in self.journeys
                     if j.learner_id == learner_id and j.external_id == external_id), None)

    async def list_steps(self, journey_ids):
        return [s for s in self.steps if s.journey_id in journey_ids]

    def add_step(self, step):
        self.steps.append(step)

    async def list_for_learner(self, learner_id):
        return [j for j in self.journeys if j.learner_id == learner_id]


def a_profile() -> LearnerProfile:
    return LearnerProfile(learner_id=LEARNER, organization_id=ORG, profile_version=2,
                          snapshot={"personal": {"email": "a@example.com"},
                                    "career": {"goal": {"target_role": "ML Engineer"}}})


def counters(**overrides) -> ProgramCounters:
    values = dict.fromkeys(
        ("files_completed", "videos_completed", "text_lessons_completed",
         "live_sessions_completed", "recorded_sessions_completed", "codelabs_completed",
         "quizzes_completed", "tasks_completed", "regular_projects_completed",
         "final_projects_completed", "peer_reviews_completed", "ai_interviews_completed"), 0)
    values.update(overrides)
    return ProgramCounters(**values)


def an_enrollment(**overrides) -> EnrollmentUpdate:
    fields = {
        "slug": "data-engineering", "public_url": "https://lms.test/journeys/data-engineering",
        "status": "in_progress", "blocked": False, "manual_added": False,
        "started_at": ENROLLED_AT, "graduated_at": None, "source_updated_at": ENROLLED_AT,
    }
    fields.update(overrides)
    return EnrollmentUpdate(**fields)


def a_progress(**overrides) -> ProgressUpdate:
    fields = {
        "progress": Decimal("0.3800"),
        "programs": (ProgramProgress(PROGRAM_ID, counters(videos_completed=3)),),
        "source_updated_at": PROGRESS_AT,
    }
    fields.update(overrides)
    return ProgressUpdate(**fields)


def an_update(**overrides) -> JourneyUpdate:
    fields = {"external_id": JOURNEY_ID, "enrollment": an_enrollment(), "progress": a_progress()}
    fields.update(overrides)
    return JourneyUpdate(**fields)


def a_sync(profile: LearnerProfile | None = None):
    learners = InMemoryLearners(profile if profile is not None else a_profile())
    journeys = InMemoryJourneys()
    return LearnerJourneySync(learners, journeys), learners, journeys


async def test_first_event_creates_the_journey_and_its_program_step():
    sync, _, journeys = a_sync()

    outcome = await sync.apply(LMS_USER_ID, an_update())

    assert outcome is SyncOutcome.APPLIED
    [journey] = journeys.journeys
    assert (journey.external_id, journey.slug, journey.status) == (
        1654, "data-engineering", "in_progress")
    assert (journey.started_at, journey.graduated_at) == (ENROLLED_AT, None)
    assert journey.progress == Decimal("0.3800")
    assert (journey.enrollment_updated_at, journey.progress_updated_at) == (
        ENROLLED_AT, PROGRESS_AT)
    [step] = journeys.steps
    assert (step.external_id, step.kind, step.title, step.status) == (
        2001668, "program", None, None)
    assert step.videos_completed == 3
    assert step.quizzes_completed == 0


async def test_progress_refresh_updates_the_existing_step():
    sync, _, journeys = a_sync()
    await sync.apply(LMS_USER_ID, an_update())
    later = datetime(2026, 9, 17, tzinfo=UTC)

    await sync.apply(LMS_USER_ID, an_update(progress=a_progress(
        progress=Decimal("0.5000"), source_updated_at=later,
        programs=(ProgramProgress(PROGRAM_ID, counters(videos_completed=7, quizzes_completed=2)),),
    )))

    [step] = journeys.steps
    assert (step.videos_completed, step.quizzes_completed) == (7, 2)
    assert journeys.journeys[0].progress == Decimal("0.5000")


async def test_new_program_adds_a_step_and_keeps_the_others():
    sync, _, journeys = a_sync()
    await sync.apply(LMS_USER_ID, an_update())

    await sync.apply(LMS_USER_ID, an_update(progress=a_progress(
        programs=(ProgramProgress(2001669, counters(tasks_completed=1)),),
    )))

    assert sorted(s.external_id for s in journeys.steps) == [2001668, 2001669]


async def test_snapshot_lists_the_journeys_and_keeps_other_sections():
    profile = a_profile()
    sync, _, _ = a_sync(profile)

    await sync.apply(LMS_USER_ID, an_update())

    assert profile.profile_version == 3
    assert profile.snapshot["personal"] == {"email": "a@example.com"}
    assert profile.snapshot["career"]["goal"] == {"target_role": "ML Engineer"}
    [entry] = profile.snapshot["career"]["journeys"]
    assert entry["external_id"] == 1654
    assert entry["progress"] == 0.38
    assert entry["started_at"] == "2026-09-16T02:18:05+00:00"
    assert entry["steps"][0]["external_id"] == 2001668
    assert entry["steps"][0]["status"] is None
    assert entry["steps"][0]["completed"]["videos_completed"] == 3


async def test_older_progress_is_skipped_while_newer_enrollment_applies():
    sync, _, journeys = a_sync()
    await sync.apply(LMS_USER_ID, an_update())
    earlier = datetime(2026, 9, 1, tzinfo=UTC)
    graduated = datetime(2026, 12, 1, tzinfo=UTC)

    outcome = await sync.apply(LMS_USER_ID, an_update(
        enrollment=an_enrollment(status="graduated", graduated_at=graduated,
                                 source_updated_at=graduated),
        progress=a_progress(progress=Decimal("0.1000"), source_updated_at=earlier),
    ))

    assert outcome is SyncOutcome.APPLIED
    journey = journeys.journeys[0]
    assert (journey.status, journey.graduated_at) == ("graduated", graduated)
    assert journey.progress == Decimal("0.3800")


async def test_fully_stale_update_changes_nothing():
    profile = a_profile()
    sync, _, journeys = a_sync(profile)
    await sync.apply(LMS_USER_ID, an_update())
    earlier = datetime(2026, 9, 1, tzinfo=UTC)

    outcome = await sync.apply(LMS_USER_ID, an_update(
        enrollment=an_enrollment(status="blocked", source_updated_at=earlier),
        progress=a_progress(source_updated_at=earlier),
    ))

    assert outcome is SyncOutcome.STALE
    assert journeys.journeys[0].status == "in_progress"
    assert profile.profile_version == 3


async def test_progress_for_an_unknown_journey_without_enrollment_is_not_found():
    """No enrollment means no status to create the journey with."""
    sync, _, journeys = a_sync()

    outcome = await sync.apply(LMS_USER_ID, an_update(enrollment=None))

    assert outcome is SyncOutcome.NOT_FOUND
    assert journeys.journeys == []


async def test_progress_only_update_applies_to_an_existing_journey():
    sync, _, journeys = a_sync()
    await sync.apply(LMS_USER_ID, an_update())

    outcome = await sync.apply(LMS_USER_ID, an_update(
        enrollment=None,
        progress=a_progress(progress=Decimal("0.9000"),
                            source_updated_at=datetime(2026, 9, 20, tzinfo=UTC)),
    ))

    assert outcome is SyncOutcome.APPLIED
    assert journeys.journeys[0].progress == Decimal("0.9000")


async def test_empty_update_is_not_found():
    sync, _, _ = a_sync()

    outcome = await sync.apply(LMS_USER_ID, an_update(enrollment=None, progress=None))

    assert outcome is SyncOutcome.NOT_FOUND


async def test_unregistered_learner_changes_nothing():
    sync, _, journeys = a_sync()

    outcome = await sync.apply(1, an_update())

    assert outcome is SyncOutcome.UNKNOWN_LEARNER
    assert journeys.journeys == []


async def test_learner_without_profile_row_is_an_error():
    sync, learners, _ = a_sync()
    learners.profile = None

    with pytest.raises(RuntimeError, match="missing its profile row"):
        await sync.apply(LMS_USER_ID, an_update())


async def test_second_journey_is_added_beside_the_first():
    profile = a_profile()
    sync, _, journeys = a_sync(profile)
    await sync.apply(LMS_USER_ID, an_update())

    await sync.apply(LMS_USER_ID, replace(an_update(), external_id=1700,
                                          progress=a_progress(programs=())))

    assert sorted(j.external_id for j in journeys.journeys) == [1654, 1700]
    assert [e["external_id"] for e in profile.snapshot["career"]["journeys"]] == [1654, 1700]
