"""LMS learner webhook follow-ups: read the learner's context and apply it, or
soft-delete a learner the LMS deleted.

Each sync is keyed on the webhook's event id, so a redelivered webhook is a
no-op once its sync succeeded. A failed read leaves nothing behind (the LMS call
happens before any write), and the IdempotentTask base retries it with backoff.
A 404 is final rather than failed: it is recorded as `not_found`, not retried.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from learner_memory.core.config import get_settings
from learner_memory.core.logging import get_logger
from learner_memory.db.repositories.journey import JourneyRepository
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.db.session import unit_of_work
from learner_memory.integrations.lms.client import LmsError, LmsNotFound, lms_client
from learner_memory.integrations.lms.mapping import to_journey_update, to_personal_info
from learner_memory.services.external_sync import SyncOutcome
from learner_memory.services.learner_deletion import LearnerDeletion
from learner_memory.services.learner_journey_sync import LearnerJourneySync
from learner_memory.services.learner_profile_sync import LearnerProfileSync
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, claim, complete, run_async

log = get_logger(__name__)

SYNC_PROFILE_TASK = "lms.sync_learner_profile"
SYNC_JOURNEY_TASK = "lms.sync_learner_journey"
DELETE_LEARNER_TASK = "lms.delete_learner"


@celery_app.task(name=SYNC_PROFILE_TASK, base=IdempotentTask)
def sync_learner_profile(event_id: str, external_id: int, organization_id: str) -> dict:
    return run_async(
        _sync_learner_profile(uuid.UUID(event_id), external_id, uuid.UUID(organization_id))
    )


async def _sync_learner_profile(
    event_id: uuid.UUID, external_id: int, organization_id: uuid.UUID
) -> dict:
    async def sync() -> SyncOutcome:
        async with lms_client(get_settings()) as lms:
            context = await lms.fetch_learner_profile(external_id)
        update = to_personal_info(context)
        async with unit_of_work() as session:
            learners = LearnerRepository(session, organization_id)
            return await LearnerProfileSync(learners).apply(external_id, update)

    args = {"event_id": str(event_id), "external_id": external_id}
    return await _run_once(SYNC_PROFILE_TASK, args, sync)


@celery_app.task(name=SYNC_JOURNEY_TASK, base=IdempotentTask)
def sync_learner_journey(event_id: str, external_id: int, organization_id: str,
                         journey_id: int) -> dict:
    return run_async(_sync_learner_journey(
        uuid.UUID(event_id), external_id, uuid.UUID(organization_id), journey_id
    ))


async def _sync_learner_journey(
    event_id: uuid.UUID, external_id: int, organization_id: uuid.UUID, journey_id: int
) -> dict:
    async def sync() -> SyncOutcome:
        async with lms_client(get_settings()) as lms:
            context = await lms.fetch_learner_journey(external_id, journey_id)
        update = to_journey_update(context, journey_id)
        async with unit_of_work() as session:
            journey_sync = LearnerJourneySync(LearnerRepository(session, organization_id),
                                              JourneyRepository(session))
            return await journey_sync.apply(external_id, update)

    args = {"event_id": str(event_id), "external_id": external_id, "journey_id": journey_id}
    return await _run_once(SYNC_JOURNEY_TASK, args, sync)


@celery_app.task(name=DELETE_LEARNER_TASK, base=IdempotentTask)
def delete_learner(event_id: str, external_id: int, organization_id: str) -> dict:
    return run_async(
        _delete_learner(uuid.UUID(event_id), external_id, uuid.UUID(organization_id))
    )


async def _delete_learner(
    event_id: uuid.UUID, external_id: int, organization_id: uuid.UUID
) -> dict:
    """Soft-delete the learner once the LMS confirms the user is gone.

    The wipe cannot be undone, so a deletion event is trusted only when the LMS
    answers 404. While it still holds the user nothing changes and the task
    fails, so it is retried with backoff (the event may have outrun the LMS's
    own commit) and dead-lettered if the user really is still there.
    """
    async def sync() -> SyncOutcome:
        async with lms_client(get_settings()) as lms:
            still_in_lms = await lms.learner_exists(external_id)
        if still_in_lms:
            raise LmsError(f"LMS still holds learner {external_id}; deletion not confirmed")
        async with unit_of_work() as session:
            deletion = LearnerDeletion(LearnerRepository(session, organization_id))
            return await deletion.apply(external_id)

    args = {"event_id": str(event_id), "external_id": external_id}
    return await _run_once(DELETE_LEARNER_TASK, args, sync)


async def _run_once(
    task: str, args: dict, sync: Callable[[], Awaitable[SyncOutcome]]
) -> dict:
    """Run `sync` unless this event's sync already succeeded; record the result
    in the idempotency ledger either way. `args` must carry the event id."""
    key = f"{task}:{args['event_id']}"
    if not await claim(task, key, args):
        log.info("lms.sync_skipped_already_done", task=task, **args)
        return {"skipped": True}

    try:
        outcome = await sync()
    except LmsNotFound:
        # Final, not a failure: the LMS dropped the learner after sending the
        # event (the deletion has its own event). Recorded so redeliveries skip.
        outcome = SyncOutcome.NOT_FOUND
    except Exception as exc:
        await complete(key, error=_describe(exc))
        raise

    await complete(key)
    log.info("lms.synced", task=task, outcome=outcome.value, **args)
    return {"outcome": outcome.value}


def _describe(exc: Exception) -> str:
    """LmsError messages are written to be safe to store. Anything else (a
    database error echoes its SQL parameters) is reduced to its type."""
    return str(exc) if isinstance(exc, LmsError) else type(exc).__name__
