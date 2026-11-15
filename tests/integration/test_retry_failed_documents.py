"""The failed-document sweep is bounded by MAX_DOC_ATTEMPTS.

Runs against real Postgres (the bound lives in SQL); only the Celery broker is
replaced, by a recorder.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from learner_memory.workers.tasks import maintenance

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def dispatched(monkeypatch) -> list[str]:
    sent: list[str] = []

    def record(name, kwargs):
        assert name == "extract.extract_document"
        sent.append(kwargs["document_id"])

    monkeypatch.setattr(maintenance.celery_app, "send_task", record)
    return sent


async def _insert_failed_document(db) -> uuid.UUID:
    doc_id = uuid.uuid4()
    async with db.unit_of_work() as s:
        await s.execute(
            text(
                "INSERT INTO raw_document (id, organization_id, source_type, content_sha256,"
                " storage_key, occurred_at, received_at, status, metadata,"
                " created_at, updated_at)"
                " VALUES (:id, :org, 'chat', :sha, 'k', now(), now(), 'failed', '{}',"
                " now(), :old)"
            ),
            {"id": doc_id, "org": ORG, "sha": doc_id.hex, "old": _past()},
        )
    return doc_id


async def _fail_again(db, doc_id: uuid.UUID) -> None:
    """What a failed extraction leaves behind: status failed, older than the cooldown."""
    async with db.unit_of_work() as s:
        await s.execute(
            text("UPDATE raw_document SET status = 'failed', updated_at = :old WHERE id = :id"),
            {"id": doc_id, "old": _past()},
        )


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


async def test_failed_document_is_redispatched_at_most_max_attempts_times(db, dispatched):
    doc_id = await _insert_failed_document(db)

    for _ in range(maintenance.MAX_DOC_ATTEMPTS + 2):
        await maintenance._retry_failed(batch_size=10)
        await _fail_again(db, doc_id)

    assert dispatched == [str(doc_id)] * 5


async def test_recently_failed_document_waits_for_the_cooldown(db, dispatched):
    doc_id = await _insert_failed_document(db)
    async with db.unit_of_work() as s:
        await s.execute(
            text("UPDATE raw_document SET updated_at = now() WHERE id = :id"), {"id": doc_id}
        )

    result = await maintenance._retry_failed(batch_size=10)

    assert result == {"dispatched": 0}
    assert dispatched == []
