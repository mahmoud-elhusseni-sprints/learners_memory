"""The ingest envelope's program/cohort scope reaches every memory card.

Ingest stores it on the document; extraction reads it back into the
ExtractionInput the extractor stamps onto each card.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from learner_memory.schemas.ingest import IngestRequest
from learner_memory.schemas.memory_card import SourceType
from learner_memory.services.ingest import IngestService
from learner_memory.workers.tasks.ingest import _extraction_input

LEARNER = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM = uuid.UUID("22222222-2222-2222-2222-222222222222")
COHORT = uuid.UUID("33333333-3333-3333-3333-333333333333")
PRODUCER_COHORT = "producer-supplied-value"


class FakeDocuments:
    def __init__(self):
        self.added = []

    async def find_duplicate(self, **_):
        return None

    async def add(self, doc):
        self.added.append(doc)
        return doc


class FakeLearners:
    async def get(self, learner_id):
        class Registered:
            id = learner_id
        return Registered()


class FakeStorage:
    async def put(self, key, body, *, content_type=None):
        return key


async def _ingest(**envelope) -> dict:
    docs = FakeDocuments()
    service = IngestService(docs, FakeLearners(), FakeStorage(), organization_id=uuid.uuid4())
    await service.ingest(
        SourceType.CHAT,
        IngestRequest(learner_id=LEARNER, occurred_at=datetime.now(UTC),
                      payload={"messages": []}, **envelope),
    )
    return docs.added[0].metadata_


async def test_ingest_stores_the_envelope_scope_without_touching_producer_metadata():
    metadata = await _ingest(
        program_id=PROGRAM, cohort_id=COHORT, metadata={"cohort_id": PRODUCER_COHORT}
    )

    assert metadata["ingest_scope"] == {"program_id": str(PROGRAM), "cohort_id": str(COHORT)}
    assert metadata["cohort_id"] == PRODUCER_COHORT


async def test_ingest_without_scope_stores_no_scope_key():
    metadata = await _ingest()

    assert "ingest_scope" not in metadata


def test_extraction_input_carries_the_stored_scope():
    snapshot = {
        "organization_id": uuid.uuid4(),
        "learner_id": LEARNER,
        "occurred_at": datetime.now(UTC),
        "metadata": {"ingest_scope": {"program_id": str(PROGRAM), "cohort_id": str(COHORT)}},
    }

    data = _extraction_input(uuid.uuid4(), snapshot, {}, False, b"{}")

    assert (data.program_id, data.cohort_id) == (PROGRAM, COHORT)
