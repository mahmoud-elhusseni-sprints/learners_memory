"""LMS learner webhook follow-up: read the part of the learner's context the event
names and apply it. One task for every kind of event; the `LmsSync` for `kind`
says what to read and how to apply it.

Keyed on the webhook's event id, so a redelivered webhook is a no-op once its
sync succeeded. A failed read leaves nothing behind (the LMS call happens before
any write), and the IdempotentTask base retries it with backoff.
"""
from __future__ import annotations

import uuid

from learner_memory.core.config import get_settings
from learner_memory.core.logging import get_logger
from learner_memory.db.session import unit_of_work
from learner_memory.integrations.lms.client import LmsError, lms_client
from learner_memory.integrations.lms.syncs import LmsSync, sync_of
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, claim, complete, run_async

log = get_logger(__name__)

SYNC_LEARNER_TASK = "lms.sync_learner"


@celery_app.task(name=SYNC_LEARNER_TASK, base=IdempotentTask)
def sync_learner(
    event_id: str, external_id: int, organization_id: str, kind: str,
    scope_id: int | None = None,
) -> dict:
    return run_async(_sync_learner(
        uuid.UUID(event_id), external_id, uuid.UUID(organization_id), sync_of(kind), scope_id
    ))


async def _sync_learner(
    event_id: uuid.UUID, external_id: int, organization_id: uuid.UUID, sync: LmsSync,
    scope_id: int | None,
) -> dict:
    key = f"{SYNC_LEARNER_TASK}:{event_id}"
    args = {"event_id": str(event_id), "external_id": external_id, "kind": sync.kind.value,
            "scope_id": scope_id}
    if not await claim(SYNC_LEARNER_TASK, key, args):
        log.info("lms.sync_skipped_already_done", **args)
        return {"skipped": True}

    try:
        async with lms_client(get_settings()) as lms:
            context = await lms.fetch_context(
                external_id, sync.context_model, sync.includes, sync.scope(scope_id)
            )
        async with unit_of_work() as session:
            outcome = await sync.apply(session, organization_id, external_id, context)
    except Exception as exc:
        await complete(key, error=_describe(exc))
        raise

    await complete(key)
    log.info("lms.synced", outcome=outcome, **args)
    return {"outcome": outcome}


def _describe(exc: Exception) -> str:
    """LmsError messages are written to be safe to store. Anything else (a
    database error echoes its SQL parameters) is reduced to its type."""
    return str(exc) if isinstance(exc, LmsError) else type(exc).__name__
