"""Shared task base: async bridge + idempotency ledger."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from celery import Task
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from learner_memory.core.logging import get_logger, new_correlation_id
from learner_memory.db.models.raw import JobRun
from learner_memory.db.session import unit_of_work

log = get_logger(__name__)


def run_async(coro):
    """Celery workers are sync; each task owns its event loop."""
    return asyncio.run(coro)


class IdempotentTask(Task):
    """Short-circuits when `idempotency_key` already succeeded.

    Every task derives its key from content (document id, learner id + dimension),
    never from time, so retries and replays converge instead of duplicating work.
    """

    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = 600
    retry_jitter = True
    max_retries = 5

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        if self.request.retries >= self.max_retries:
            run_async(_dead_letter(self.name, {"args": args, "kwargs": kwargs}, str(exc)))
        log.error("task.failed", task=self.name, error=str(exc), retries=self.request.retries)


async def claim(task: str, idempotency_key: str, args: dict[str, Any]) -> bool:
    """True if this run owns the key; False if it already succeeded."""
    async with unit_of_work() as s:
        existing = (
            await s.execute(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing and existing.status == "succeeded":
            return False
        await s.execute(
            insert(JobRun)
            .values(task=task, idempotency_key=idempotency_key, args=args, status="running",
                    attempts=1, started_at=datetime.now(UTC))
            .on_conflict_do_update(
                index_elements=[JobRun.idempotency_key],
                set_={"status": "running", "attempts": JobRun.attempts + 1,
                      "started_at": datetime.now(UTC)},
            )
        )
    new_correlation_id()
    return True


async def complete(idempotency_key: str, *, error: str | None = None) -> None:
    async with unit_of_work() as s:
        job = (
            await s.execute(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if job:
            job.status = "failed" if error else "succeeded"
            job.finished_at = datetime.now(UTC)
            job.error = {"message": error} if error else None


async def _dead_letter(task: str, args: dict, error: str) -> None:
    from learner_memory.db.models.raw import DeadLetter

    async with unit_of_work() as s:
        s.add(DeadLetter(task=task, args=args, error={"message": error}))
