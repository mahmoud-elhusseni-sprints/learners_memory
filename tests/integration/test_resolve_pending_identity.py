"""Parked documents are released once their learner registers.

Real Postgres (the matching is SQL over JSONB); only the Celery broker is
replaced, by a recorder.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from learner_memory.db.models.raw import RawDocument
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.workers.tasks import maintenance

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
MENTOR_NEVER_REGISTERED = uuid.UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture
def dispatched(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        maintenance.celery_app, "send_task", lambda name, kwargs: sent.append(kwargs["document_id"])
    )
    return sent


async def _park(db, source_type: str, metadata: dict, org: uuid.UUID = ORG) -> uuid.UUID:
    doc_id = uuid.uuid4()
    async with db.unit_of_work() as s:
        s.add(RawDocument(
            id=doc_id, organization_id=org, source_type=source_type,
            content_sha256=doc_id.hex, storage_key="k", occurred_at=_now(),
            received_at=_now(), status="pending_identity", metadata_=metadata,
        ))
    return doc_id


async def _register(db, learner_id: uuid.UUID, org: uuid.UUID = ORG) -> None:
    async with db.unit_of_work() as s:
        await LearnerRepository(s, org).register(learner_id=learner_id)


async def _load(db, doc_id: uuid.UUID) -> RawDocument:
    async with db.unit_of_work() as s:
        return (await s.execute(select(RawDocument).where(RawDocument.id == doc_id))).scalar_one()


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def test_single_learner_document_is_attached_and_dispatched_after_registration(
    db, dispatched
):
    doc_id = await _park(db, "chat", {"unresolved_learner_id": str(LEARNER)})
    await _register(db, LEARNER)

    result = await maintenance._resolve_pending_identity(batch_size=10)

    doc = await _load(db, doc_id)
    assert result == {"released": 1}
    assert dispatched == [str(doc_id)]
    assert (doc.status, doc.learner_id) == ("received", LEARNER)
    assert "unresolved_learner_id" not in doc.metadata_


async def test_meeting_gains_the_registered_participant_and_keeps_the_rest_unresolved(
    db, dispatched
):
    doc_id = await _park(db, "meeting_transcript", {
        "participants": {},
        "unresolved_participants": {
            "SPEAKER_01": str(LEARNER), "SPEAKER_02": str(MENTOR_NEVER_REGISTERED),
        },
    })
    await _register(db, LEARNER)

    await maintenance._resolve_pending_identity(batch_size=10)

    doc = await _load(db, doc_id)
    assert dispatched == [str(doc_id)]
    assert doc.status == "received"
    assert doc.metadata_["participants"] == {"SPEAKER_01": str(LEARNER)}
    assert doc.metadata_["unresolved_participants"] == {
        "SPEAKER_02": str(MENTOR_NEVER_REGISTERED)
    }


async def test_document_stays_parked_while_its_learner_is_unregistered(db, dispatched):
    doc_id = await _park(db, "chat", {"unresolved_learner_id": str(LEARNER)})

    result = await maintenance._resolve_pending_identity(batch_size=10)

    assert result == {"released": 0}
    assert dispatched == []
    assert (await _load(db, doc_id)).status == "pending_identity"


async def test_learner_registered_in_another_org_does_not_release_the_document(db, dispatched):
    doc_id = await _park(db, "chat", {"unresolved_learner_id": str(LEARNER)})
    await _register(db, LEARNER, org=OTHER_ORG)

    await maintenance._resolve_pending_identity(batch_size=10)

    assert dispatched == []
    assert (await _load(db, doc_id)).status == "pending_identity"


async def test_released_document_is_not_dispatched_twice(db, dispatched):
    doc_id = await _park(db, "chat", {"unresolved_learner_id": str(LEARNER)})
    await _register(db, LEARNER)

    await maintenance._resolve_pending_identity(batch_size=10)
    await maintenance._resolve_pending_identity(batch_size=10)

    assert dispatched == [str(doc_id)]
    async with db.unit_of_work() as s:
        count = (await s.execute(text("SELECT count(*) FROM raw_document"))).scalar_one()
    assert count == 1
