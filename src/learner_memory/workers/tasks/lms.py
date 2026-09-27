"""LMS learner webhook follow-up: read the learner's context and apply it.

Keyed on the webhook's event id, so a redelivered webhook is a no-op once its
sync succeeded. A failed read leaves nothing behind (the LMS call happens before
any write), and the IdempotentTask base retries it with backoff.
"""
from __future__ import annotations

import uuid

from learner_memory.core.config import get_settings
from learner_memory.core.logging import get_logger
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.db.session import unit_of_work
from learner_memory.integrations.lms.client import LmsError, lms_client
from learner_memory.integrations.lms.mapping import to_personal_info
from learner_memory.services.learner_profile_sync import LearnerProfileSync
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, claim, complete, run_async

log = get_logger(__name__)

SYNC_PROFILE_TASK = "lms.sync_learner_profile"


@celery_app.task(name=SYNC_PROFILE_TASK, base=IdempotentTask)
def sync_learner_profile(event_id: str, external_id: int, organization_id: str) -> dict:
    return run_async(
        _sync_learner_profile(uuid.UUID(event_id), external_id, uuid.UUID(organization_id))
    )


async def _sync_learner_profile(
    event_id: uuid.UUID, external_id: int, organization_id: uuid.UUID
) -> dict:
    key = f"{SYNC_PROFILE_TASK}:{event_id}"
    args = {"event_id": str(event_id), "external_id": external_id}
    if not await claim(SYNC_PROFILE_TASK, key, args):
        log.info("lms.profile_sync_skipped_already_done", **args)
        return {"skipped": True}

    try:
        async with lms_client(get_settings()) as lms:
            context = await lms.fetch_learner_profile(external_id)
        update = to_personal_info(context)
        async with unit_of_work() as session:
            sync = LearnerProfileSync(LearnerRepository(session, organization_id))
            outcome = await sync.apply(external_id, update)
    except Exception as exc:
        await complete(key, error=_describe(exc))
        raise

    await complete(key)
    log.info("lms.profile_synced", outcome=outcome.value, **args)
    return {"outcome": outcome.value}


def _describe(exc: Exception) -> str:
    """LmsError messages are written to be safe to store. Anything else (a
    database error echoes its SQL parameters) is reduced to its type."""
    return str(exc) if isinstance(exc, LmsError) else type(exc).__name__
