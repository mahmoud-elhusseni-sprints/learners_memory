"""Roster resolution at ingest, and the per-learner fan-out in the worker.

These cover the multi-learner plumbing around the extractor: which participants
survive registration filtering, and how produced cards are grouped back to the
learners whose profiles they should recompute.
"""
from __future__ import annotations

import uuid

from learner_memory.schemas.memory_card import Contribution, MemoryCard, SourceType
from learner_memory.services.ingest import IngestService
from learner_memory.workers.tasks.ingest import _dimensions_by_learner, _participants

LEARNER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
UNREGISTERED = uuid.UUID("99999999-9999-9999-9999-999999999999")


class FakeLearners:
    """Stands in for LearnerRepository.get — only the registered ids resolve."""

    def __init__(self, registered: set[uuid.UUID]):
        self._registered = registered

    async def get(self, learner_id: uuid.UUID):
        return object() if learner_id in self._registered else None


def _service(registered: set[uuid.UUID]) -> IngestService:
    return IngestService(
        documents=None, learners=FakeLearners(registered), storage=None,
        organization_id=uuid.uuid4(),
    )


async def test_resolve_roster_keeps_only_registered_learners():
    service = _service({LEARNER_A})
    roster = await service._resolve_roster(
        {"SPEAKER_01": str(LEARNER_A), "SPEAKER_02": str(UNREGISTERED)}
    )
    assert roster == {"SPEAKER_01": LEARNER_A}


async def test_resolve_roster_drops_malformed_ids_without_failing():
    service = _service({LEARNER_A})
    roster = await service._resolve_roster({"SPEAKER_01": str(LEARNER_A), "SPEAKER_02": "not-a-uuid"})
    assert roster == {"SPEAKER_01": LEARNER_A}


async def test_resolve_identity_parks_meeting_with_no_registered_participants():
    service = _service(set())
    owner, status, meta = await service._resolve_identity(
        _req(participants={"SPEAKER_01": str(UNREGISTERED)}), multi_learner=True
    )
    assert owner is None
    assert status == "pending_identity"
    assert meta["participants"] == {}


async def test_resolve_identity_receives_meeting_with_a_registered_participant():
    service = _service({LEARNER_A})
    owner, status, meta = await service._resolve_identity(
        _req(participants={"SPEAKER_01": str(LEARNER_A)}), multi_learner=True
    )
    assert owner is None                       # a meeting has no single owner
    assert status == "received"
    assert meta["participants"] == {"SPEAKER_01": str(LEARNER_A)}


def _req(*, participants: dict):
    from learner_memory.schemas.ingest import IngestRequest

    return IngestRequest(
        occurred_at="2026-03-01T00:00:00Z",
        payload={"segments": []},
        metadata={"participants": participants},
    )


# ------------------------------------------------------------- fan-out


def test_participants_parses_stored_roster_into_uuids():
    parsed = _participants({"participants": {"SPEAKER_01": str(LEARNER_A)}})
    assert parsed == {"SPEAKER_01": LEARNER_A}


def test_participants_is_empty_when_no_roster_stored():
    assert _participants({}) == {}


def test_dimensions_are_grouped_per_learner():
    cards = [_card(LEARNER_A, "collaboration"), _card(LEARNER_A, "communication"),
             _card(LEARNER_B, "planning")]
    grouped = _dimensions_by_learner(cards)

    assert grouped[LEARNER_A] == {"general_skill:collaboration", "general_skill:communication"}
    assert grouped[LEARNER_B] == {"general_skill:planning"}


def _card(learner_id: uuid.UUID, skill: str) -> MemoryCard:
    return MemoryCard(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), learner_id=learner_id,
        source_type=SourceType.MEETING_TRANSCRIPT, source_document_id=uuid.uuid4(),
        ingested_at="2026-03-01T00:00:00Z", extractor_version="meeting_transcript@1.0",
        prompt_version="v1", model="test",
        card_type="observation", title="t", content="c",
        observed_at="2026-03-01T00:00:00Z",
        contributions=[Contribution(target="general_skill", key=skill, level_signal=3)],
    )
