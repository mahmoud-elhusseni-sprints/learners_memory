"""Apply a learner's LMS journey enrollment and progress to their learning journey.

The LMS owns these fields: its journey is the learner's learning journey, and
each program in it is a journey step. Enrollment and progress carry separate LMS
change times, so each part is applied only if it is not older than the last.
Programs are only ever added or updated, never removed: the LMS answer is
filtered to one journey and says nothing about what it left out.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from decimal import Decimal

from learner_memory.db.models.learner import JourneyStep, LearningJourney
from learner_memory.db.repositories.journey import JourneyRepository
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.services.external_sync import SyncOutcome, is_stale

CAREER_SECTION = "career"
JOURNEYS_KEY = "journeys"
PROGRAM_STEP_KIND = "program"


@dataclass(frozen=True, slots=True)
class ProgramCounters:
    """Items completed in a program. Field names are the journey_step columns."""

    files_completed: int
    videos_completed: int
    text_lessons_completed: int
    live_sessions_completed: int
    recorded_sessions_completed: int
    codelabs_completed: int
    quizzes_completed: int
    tasks_completed: int
    regular_projects_completed: int
    final_projects_completed: int
    peer_reviews_completed: int
    ai_interviews_completed: int


COUNTER_COLUMNS = tuple(field.name for field in fields(ProgramCounters))


@dataclass(frozen=True, slots=True)
class ProgramProgress:
    external_id: int
    counters: ProgramCounters


@dataclass(frozen=True, slots=True)
class EnrollmentUpdate:
    slug: str | None
    public_url: str | None
    status: str
    blocked: bool
    manual_added: bool
    started_at: datetime | None
    graduated_at: datetime | None
    source_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    progress: Decimal                  # 0..1 fraction
    programs: tuple[ProgramProgress, ...]
    source_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class JourneyUpdate:
    """One LMS journey as the LMS currently holds it; either part may be absent."""

    external_id: int
    enrollment: EnrollmentUpdate | None
    progress: ProgressUpdate | None


class LearnerJourneySync:
    def __init__(self, learners: LearnerRepository, journeys: JourneyRepository) -> None:
        self._learners = learners
        self._journeys = journeys

    async def apply(self, learner_external_id: int, update: JourneyUpdate) -> SyncOutcome:
        """Write `update` to the learner holding `learner_external_id`, within the
        caller's transaction.

        NOT_FOUND when the update is empty, or names a journey we do not have yet
        without the enrollment needed to create it. Raises RuntimeError if the
        learner lacks the profile row that registration always creates.
        """
        learner = await self._learners.get_by_external_id(learner_external_id)
        if learner is None:
            return SyncOutcome.UNKNOWN_LEARNER

        journey = await self._lock_journey(learner.id, update)
        if journey is None:
            return SyncOutcome.NOT_FOUND

        enrollment_applied = _apply_enrollment(journey, update.enrollment)
        progress_applied = await self._apply_progress(journey, update.progress)
        if not (enrollment_applied or progress_applied):
            return SyncOutcome.STALE

        await self._refresh_snapshot(learner.id)
        return SyncOutcome.APPLIED

    async def _lock_journey(self, learner_id: uuid.UUID,
                            update: JourneyUpdate) -> LearningJourney | None:
        if update.enrollment is None and update.progress is None:
            return None
        if update.enrollment is not None:
            await self._journeys.create_if_missing(learner_id, update.external_id,
                                                   update.enrollment.status)
        return await self._journeys.lock_by_external_id(learner_id, update.external_id)

    async def _apply_progress(self, journey: LearningJourney,
                              progress: ProgressUpdate | None) -> bool:
        if progress is None or is_stale(journey.progress_updated_at, progress.source_updated_at):
            return False
        journey.progress = progress.progress
        journey.progress_updated_at = progress.source_updated_at

        existing = {step.external_id: step for step in await self._journeys.list_steps([journey.id])}
        for program in progress.programs:
            step = existing.get(program.external_id)
            if step is None:
                step = _new_program_step(journey.id, program.external_id)
                self._journeys.add_step(step)
            for column, value in asdict(program.counters).items():
                setattr(step, column, value)
        return True

    async def _refresh_snapshot(self, learner_id: uuid.UUID) -> None:
        """Rebuild the snapshot's journeys from the tables.

        The profile lock is taken before reading the journeys, so a concurrent sync
        of another journey has committed by the time they are read.
        """
        profile = await self._learners.lock_profile(learner_id)
        if profile is None:
            raise RuntimeError(
                f"learner {learner_id} is missing its profile row; "
                "re-register the learner to recreate it"
            )
        journeys = await self._journeys.list_for_learner(learner_id)
        steps = await self._journeys.list_steps([journey.id for journey in journeys])
        career = {**profile.snapshot.get(CAREER_SECTION, {}),
                  JOURNEYS_KEY: journeys_section(journeys, steps)}
        # Reassign rather than mutate: JSONB changes are only tracked on assignment.
        profile.snapshot = {**profile.snapshot, CAREER_SECTION: career}
        profile.profile_version += 1


def _apply_enrollment(journey: LearningJourney, enrollment: EnrollmentUpdate | None) -> bool:
    if enrollment is None or is_stale(journey.enrollment_updated_at,
                                      enrollment.source_updated_at):
        return False
    journey.slug = enrollment.slug
    journey.public_url = enrollment.public_url
    journey.status = enrollment.status
    journey.blocked = enrollment.blocked
    journey.manual_added = enrollment.manual_added
    journey.started_at = enrollment.started_at
    journey.graduated_at = enrollment.graduated_at
    journey.enrollment_updated_at = enrollment.source_updated_at
    return True


def _new_program_step(journey_id: uuid.UUID, program_id: int) -> JourneyStep:
    """Status stays null: without totals the LMS counters cannot say a program is done."""
    return JourneyStep(journey_id=journey_id, external_id=program_id, kind=PROGRAM_STEP_KIND,
                       ord=0, title=None, status=None, evidence_card_ids=[], details={})


def journeys_section(journeys: list[LearningJourney], steps: list[JourneyStep]) -> list[dict]:
    """The snapshot's `career.journeys`: every journey with its steps."""
    steps_by_journey: dict[uuid.UUID, list[JourneyStep]] = {}
    for step in steps:
        steps_by_journey.setdefault(step.journey_id, []).append(step)
    return [_journey_entry(journey, steps_by_journey.get(journey.id, [])) for journey in journeys]


def _journey_entry(journey: LearningJourney, steps: list[JourneyStep]) -> dict:
    return {
        "external_id": journey.external_id,
        "name": journey.name,
        "slug": journey.slug,
        "public_url": journey.public_url,
        "status": journey.status,
        "blocked": journey.blocked,
        "manual_added": journey.manual_added,
        "started_at": _iso(journey.started_at),
        "graduated_at": _iso(journey.graduated_at),
        # JSON has no decimal type; a float is exact enough for a 4-place fraction.
        "progress": float(journey.progress),
        "steps": [_step_entry(step) for step in steps],
    }


def _step_entry(step: JourneyStep) -> dict:
    return {
        "external_id": step.external_id,
        "kind": step.kind,
        "title": step.title,
        "status": step.status,
        "completed": {column: getattr(step, column) for column in COUNTER_COLUMNS},
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
