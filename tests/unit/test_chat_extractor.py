"""Chat extraction: metadata normalization, timing, provenance and agent flow."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from learner_memory.extractors.base import ExtractionInput
from learner_memory.extractors.chunking import Chunk
from learner_memory.extractors.registry import get_extractor, load_extractors
from learner_memory.schemas.ingest import IngestRequest
from learner_memory.schemas.memory_card import (
    Contribution,
    MemoryCardDraft,
    SourceType,
)

LEARNER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

MESSAGES = [
    {
        "text": "Can you send me a progress update?",
        "sender": {"id": "manager-1", "role": "manager"},
        "timestamp": "2026-10-01T09:00:00Z",
        "attachments": [],
    },
    {
        "text": "Yes, I finished the API and will send the link.",
        "sender": {"id": "learner-7", "role": "learner"},
        "timestamp": "2026-10-01T09:12:00Z",
        "attachments": [{"name": "status.pdf", "type": "application/pdf"}],
        "metadata": {"thread_id": "thread-4"},
    },
    {
        "text": "Please also book your mentor check-in.",
        "sender": "mentor",
        "timestamp": "2026-10-02T10:00:00Z",
    },
]


def _input(payload=MESSAGES, *, occurred_at="2026-10-05T10:00:00Z", metadata=None):
    return ExtractionInput(
        document_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        organization_id=uuid.uuid4(),
        learner_id=LEARNER_ID,
        occurred_at=datetime.fromisoformat(occurred_at),
        raw=json.dumps(payload).encode(),
        metadata=metadata or {"conversation_id": "conversation-9", "channel": "slack"},
    )


def _extractor():
    load_extractors()
    return get_extractor(SourceType.CHAT)


def _draft(quote="Yes, I finished the API and will send the link."):
    return MemoryCardDraft(
        card_type="observation",
        title="Responded promptly with a concrete update",
        content="The learner replied to their manager within 12 minutes with a concrete update.",
        evidence_quote=quote,
        contributions=[Contribution(target="general_skill", key="communication", level_signal=2)],
    )


def test_parse_renders_sender_timestamp_attachments_and_response_timing():
    text = _extractor().parse(_input())

    assert "message:0" in text
    assert "sender_role=manager" in text
    assert "learner_reply=1" in text
    assert "learner_reply_after=12 minutes" in text
    assert "learner_reply_to=0" in text
    assert "learner_reply_delay=12 minutes" in text
    assert 'attachments=[{"name":"status.pdf","type":"application/pdf"}]' in text
    assert 'metadata={"thread_id":"thread-4"}' in text


def test_unanswered_message_uses_snapshot_time_without_claiming_intent():
    text = _extractor().parse(_input())
    mentor_line = next(line for line in text.splitlines() if "sender_role=mentor" in line)

    assert "learner_reply=none_in_transcript" in mentor_line
    assert "unanswered_for=3 days" in mentor_line
    assert "ignored" not in mentor_line


def test_messages_are_ordered_by_timestamp_not_input_order():
    text = _extractor().parse(_input(list(reversed(MESSAGES))))
    assert text.splitlines()[0].endswith("Can you send me a progress update?")


def test_multiline_message_remains_one_indexed_turn():
    payload = [
        {
            "text": "First line\nSecond line",
            "sender": "learner",
            "time": "2026-10-01T09:00:00Z",
        }
    ]
    text = _extractor().parse(_input(payload))

    assert text.count("\n") == 0
    assert text.endswith("First line Second line")


def test_learner_sender_id_supports_non_role_sender_labels():
    payload = [
        {"text": "Status?", "sender": "manager", "time": "2026-10-01T09:00:00Z"},
        {"text": "Done", "sender": "user-42", "time": "2026-10-01T09:05:00Z"},
    ]
    text = _extractor().parse(_input(payload, metadata={"learner_sender_id": "user-42"}))
    assert "learner_reply_delay=5 minutes" in text


def test_payload_requires_a_list_or_messages_wrapper():
    with pytest.raises(ValueError, match="JSON list"):
        _extractor().parse(_input({"items": MESSAGES}))


def test_ingest_envelope_accepts_the_requested_bare_message_list():
    request = IngestRequest(
        learner_id=LEARNER_ID,
        occurred_at="2026-10-05T10:00:00Z",
        payload=MESSAGES,
    )
    assert request.payload == MESSAGES


def test_empty_text_and_attachments_is_rejected():
    payload = [
        {"text": " ", "attachments": [], "sender": "learner", "time": "2026-10-01T09:00:00Z"}
    ]
    with pytest.raises(ValidationError, match="text or at least one attachment"):
        _extractor().parse(_input(payload))


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return [_draft()]


async def test_extract_chunk_stamps_chat_provenance():
    ext = _extractor()
    data = _input()
    text = ext.parse(data)
    agent = FakeAgent()
    ext._agent = agent

    drafts = await ext._extract_chunk(data, Chunk(anchor="turn:0-2", text=text))
    payload = drafts[0].payload

    assert agent.calls[0]["chunk"] == text
    assert agent.calls[0]["context"]["conversation_id"] == "conversation-9"
    assert payload["conversation_id"] == "conversation-9"
    assert payload["channel"] == "slack"
    assert payload["message_range"] == "turn:0-2"
    assert payload["message_count"] == 3
    assert payload["participant_roles"] == ["learner", "manager", "mentor"]
    assert payload["started_at"] == "2026-10-01T09:00:00Z"
    assert payload["ended_at"] == "2026-10-02T10:00:00Z"


def test_stamp_assigns_chat_card_to_envelope_learner():
    ext = _extractor()
    data = _input()
    card = ext._stamp(data, Chunk(anchor="turn:0-2", text="source"), _draft())

    assert card is not None
    assert card.learner_id == LEARNER_ID
    assert card.source_type == SourceType.CHAT
