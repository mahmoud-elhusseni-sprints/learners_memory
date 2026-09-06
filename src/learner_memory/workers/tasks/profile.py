"""Profile synthesis tasks.

`schedule_recompute` coalesces: a meeting that yields 20 cards marks the affected
dimensions stale once and lets the 15-minute beat (or the debounce window) drive a
single recompute, instead of 20 competing ones.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from learner_memory.core.logging import get_logger
from learner_memory.db.models.learner import LearnerProfile
from learner_memory.db.session import unit_of_work
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, run_async

log = get_logger(__name__)


@celery_app.task(name="profile.schedule_recompute", base=IdempotentTask)
def schedule_recompute(learner_id: str, dimensions: list[str]) -> dict:
    return run_async(_mark_stale(uuid.UUID(learner_id), dimensions))


async def _mark_stale(learner_id: uuid.UUID, dimensions: list[str]) -> dict:
    async with unit_of_work() as s:
        profile = (
            await s.execute(select(LearnerProfile).where(LearnerProfile.learner_id == learner_id))
        ).scalar_one_or_none()
        if profile is None:
            log.warning("profile.unknown_learner", learner_id=str(learner_id))
            return {"skipped": True}
        profile.stale_dimensions = sorted(set(profile.stale_dimensions or []) | set(dimensions))
    return {"learner_id": str(learner_id), "stale": dimensions}


@celery_app.task(name="profile.recompute_profile", base=IdempotentTask)
def recompute_profile(learner_id: str, dimensions: list[str] | None = None) -> dict:
    return run_async(_recompute(uuid.UUID(learner_id), dimensions))


async def _recompute(learner_id: uuid.UUID, dimensions: list[str] | None) -> dict:
    # TODO(profile): synthesizer not implemented yet — see
    # docs/architecture/05-pipelines.md §5.3. Retrieval, weighting and the
    # skill_assessment write land with the profile milestone.
    raise NotImplementedError("profile synthesizer pending")


@celery_app.task(name="profile.refresh_stale_profiles", base=IdempotentTask)
def refresh_stale_profiles(batch_size: int = 200) -> dict:
    return run_async(_refresh_stale(batch_size))


async def _refresh_stale(batch_size: int) -> dict:
    async with unit_of_work() as s:
        rows = (
            await s.execute(
                select(LearnerProfile.learner_id, LearnerProfile.stale_dimensions)
                .where(LearnerProfile.stale_dimensions != [])
                .limit(batch_size)
            )
        ).all()

    for learner_id, dims in rows:
        celery_app.send_task(
            "profile.recompute_profile",
            kwargs={"learner_id": str(learner_id), "dimensions": list(dims)},
        )
    log.info("profile.refresh_dispatched", count=len(rows), at=datetime.now(UTC).isoformat())
    return {"dispatched": len(rows)}
