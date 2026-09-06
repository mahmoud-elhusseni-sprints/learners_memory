import uuid
from datetime import UTC, datetime

from learner_memory.schemas.memory_card import Contribution, MemoryCard
from learner_memory.storage.paths import object_key


def test_card_ids_are_deterministic_so_reextraction_upserts():
    doc = uuid.uuid4()
    assert MemoryCard.deterministic_id(doc, "turn:0-40:Asked for evidence") == \
           MemoryCard.deterministic_id(doc, "turn:0-40:Asked for evidence")
    assert MemoryCard.deterministic_id(doc, "turn:0-40:a") != \
           MemoryCard.deterministic_id(doc, "turn:41-80:a")


def test_contribution_index_key_is_what_qdrant_filters_on():
    c = Contribution(target="general_skill", key="critical_thinking", level_signal=4)
    assert c.index_key == "general_skill:critical_thinking"


def test_object_key_is_deterministic_and_org_scoped():
    org, doc = uuid.uuid4(), uuid.uuid4()
    when = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    key = object_key(organization_id=org, source_type="cv", document_id=doc,
                     occurred_at=when, mime_type="application/pdf")
    assert key == f"org/{org}/cv/2026/09/06/{doc}.pdf"
