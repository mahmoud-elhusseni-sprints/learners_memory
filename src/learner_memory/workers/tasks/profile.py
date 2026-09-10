"""Profile synthesis tasks.

`schedule_recompute` coalesces: a meeting that yields 20 cards marks the affected
dimensions stale once and lets the 15-minute beat (or the debounce window) drive a
single recompute, instead of 20 competing ones.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from learner_memory.core.logging import get_logger
from learner_memory.db.repositories.profile import ProfileRepository
from learner_memory.db.session import unit_of_work
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, run_async

log = get_logger(__name__)


@celery_app.task(name="profile.schedule_recompute", base=IdempotentTask)
def schedule_recompute(organization_id: str, learner_id: str, dimensions: list[str]) -> dict:
    return run_async(
        _mark_stale(uuid.UUID(organization_id), uuid.UUID(learner_id), dimensions)
    )


async def _mark_stale(
    organization_id: uuid.UUID, learner_id: uuid.UUID, dimensions: list[str]
) -> dict:
    async with unit_of_work() as s:
        repository = ProfileRepository(s, organization_id)
        if not await repository.mark_stale(learner_id, dimensions):
            log.warning("profile.unknown_learner", learner_id=str(learner_id))
            return {"skipped": True}
    return {"learner_id": str(learner_id), "stale": dimensions}


@celery_app.task(name="profile.recompute_profile", base=IdempotentTask)
def recompute_profile(
    organization_id: str, learner_id: str, dimensions: list[str] | None = None
) -> dict:
    return run_async(
        _recompute(uuid.UUID(organization_id), uuid.UUID(learner_id), dimensions)
    )


async def _recompute(
    organization_id: uuid.UUID,
    learner_id: uuid.UUID,
    dimensions: list[str] | None,
) -> dict:
    # Import lazily so worker startup remains cheap and task discovery does not
    # construct the LLM client before the worker process is initialized.
    from learner_memory.profile.service import recompute_profile as run_recompute

    return await run_recompute(organization_id, learner_id, dimensions)


@celery_app.task(name="profile.refresh_stale_profiles", base=IdempotentTask)
def refresh_stale_profiles(batch_size: int = 200) -> dict:
    return run_async(_refresh_stale(batch_size))


async def _refresh_stale(batch_size: int) -> dict:
    async with unit_of_work() as s:
        rows = await ProfileRepository.stale_profiles(s, batch_size)

    for organization_id, learner_id, dims in rows:
        celery_app.send_task(
            "profile.recompute_profile",
            kwargs={
                "organization_id": str(organization_id),
                "learner_id": str(learner_id),
                "dimensions": list(dims),
            },
        )
    log.info("profile.refresh_dispatched", count=len(rows), at=datetime.now(UTC).isoformat())
    return {"dispatched": len(rows)}
