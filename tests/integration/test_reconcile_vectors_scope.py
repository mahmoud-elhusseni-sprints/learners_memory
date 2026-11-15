"""Re-indexing a card keeps the program scope of its source document.

Real Postgres for the ledger join; the embedding API and Qdrant are replaced at
their boundaries by recorders.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from learner_memory.db.models.raw import MemoryCardRecord, RawDocument
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.vector.qdrant import to_payload
from learner_memory.workers.tasks import maintenance

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
PROGRAM = uuid.UUID("55555555-5555-5555-5555-555555555555")


class RecordingEmbedder:
    async def embed(self, texts, *, trace):
        return [[0.0] * 4 for _ in texts]


class RecordingIndex:
    def __init__(self):
        self.payloads = []

    async def upsert(self, cards, vectors):
        self.payloads.extend(to_payload(c) for c in cards)


async def _unsynced_card_from_scoped_document(db) -> None:
    now = datetime.now(UTC)
    doc_id = uuid.uuid4()
    async with db.unit_of_work() as s:
        await LearnerRepository(s, ORG).register(learner_id=LEARNER)
        s.add(RawDocument(
            id=doc_id, organization_id=ORG, learner_id=LEARNER, source_type="chat",
            content_sha256=doc_id.hex, storage_key="k", occurred_at=now, received_at=now,
            status="extracted", metadata_={"ingest_scope": {"program_id": str(PROGRAM)}},
        ))
        await s.flush()
        s.add(MemoryCardRecord(
            id=uuid.uuid4(), organization_id=ORG, learner_id=LEARNER,
            source_document_id=doc_id, source_type="chat", card_type="observation",
            title="t", content="c", observed_at=now, extractor_version="v1",
            prompt_version="v1", model="m",
        ))


async def test_reconciled_card_keeps_its_document_program_id(db, monkeypatch):
    from learner_memory.llm import client as llm_module
    from learner_memory.vector import qdrant as qdrant_module

    index = RecordingIndex()
    monkeypatch.setattr(llm_module, "get_llm_client", lambda: RecordingEmbedder())
    monkeypatch.setattr(qdrant_module, "get_card_index", lambda: index)
    await _unsynced_card_from_scoped_document(db)

    result = await maintenance._reconcile(batch_size=10)

    assert result == {"resynced": 1}
    assert [p["program_id"] for p in index.payloads] == [str(PROGRAM)]
