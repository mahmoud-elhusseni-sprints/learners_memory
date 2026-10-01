"""Meeting transcript: parsing, per-learner attribution, and the LangGraph agent.

Attribution is the whole point of a multi-learner source, so most of these tests
exercise the boundary between the agent (which only knows speaker labels) and
BaseExtractor._stamp (which turns a label into a learner id, or drops the card).
The agent is driven by a fake LLM so the graph's control flow runs without a
network call.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from learner_memory.extractors.agents.meeting_transcript import (
    MeetingAnalysis,
    MeetingTranscriptAgent,
    ParticipantObservation,
)
from learner_memory.extractors.base import ExtractionInput
from learner_memory.extractors.chunking import Chunk, TranscriptChunker
from learner_memory.extractors.registry import get_extractor, load_extractors
from learner_memory.llm.client import TraceContext
from learner_memory.schemas.memory_card import (
    Contribution,
    ExtractionResult,
    MemoryCardDraft,
    SourceType,
)

LEARNER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

TRANSCRIPT = {
    "segments": [
        {"speaker": "SPEAKER_01", "text": "I finished the auth migration and unblocked Sam's PR."},
        {"speaker": "SPEAKER_02", "text": "I'm still stuck on the flaky test, could use a hand."},
        {"speaker": "MENTOR", "text": "Great, let's pair after standup."},
        {"speaker": "SPEAKER_01", "text": "Happy to pair on the flaky test this afternoon."},
    ]
}


def _input(raw: bytes, participants: dict[str, uuid.UUID]) -> ExtractionInput:
    return ExtractionInput(
        document_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        organization_id=uuid.uuid4(),
        learner_id=None,
        participants=participants,
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
        raw=raw,
        metadata={"meeting_type": "standup", "title": "Daily standup"},
    )


def _extractor():
    load_extractors()
    return get_extractor(SourceType.MEETING_TRANSCRIPT)


# ------------------------------------------------------------------ parsing


def test_parse_renders_diarized_segments_as_speaker_turns():
    text = _extractor().parse(_input(json.dumps(TRANSCRIPT).encode(), {}))

    assert "SPEAKER_01: I finished the auth migration and unblocked Sam's PR." in text
    assert "SPEAKER_02: I'm still stuck on the flaky test, could use a hand." in text
    assert "MENTOR: Great, let's pair after standup." in text


def test_parse_skips_empty_turns():
    doc = {"segments": [{"speaker": "S1", "text": "hello"}, {"speaker": "S2", "text": "  "}]}
    text = _extractor().parse(_input(json.dumps(doc).encode(), {}))

    assert "S1: hello" in text
    assert "S2" not in text


def test_parse_falls_back_to_plain_text():
    text = _extractor().parse(_input(b"SPEAKER_01: just plain text", {}))
    assert text == "SPEAKER_01: just plain text"


# --------------------------------------------------------------- attribution


def _draft(subject: str | None, title="Unblocked a teammate", quote=None):
    return MemoryCardDraft(
        card_type="observation", title=title, subject_label=subject,
        content="The learner proactively unblocked a peer's work.", evidence_quote=quote,
        contributions=[Contribution(target="general_skill", key="collaboration", level_signal=3)],
    )


def test_stamp_resolves_speaker_label_to_the_registered_learner():
    ext = _extractor()
    data = _input(json.dumps(TRANSCRIPT).encode(), {"SPEAKER_01": LEARNER_A, "SPEAKER_02": LEARNER_B})
    chunk = Chunk(anchor="turn:0-3", text="x")

    card = ext._stamp(data, chunk, _draft("SPEAKER_01"))

    assert card is not None
    assert card.learner_id == LEARNER_A


def test_stamp_drops_a_card_attributed_to_a_speaker_not_in_the_roster():
    """A mentor, an unregistered learner, or a hallucinated label must never get a card."""
    ext = _extractor()
    data = _input(json.dumps(TRANSCRIPT).encode(), {"SPEAKER_01": LEARNER_A})
    chunk = Chunk(anchor="turn:0-3", text="x")

    assert ext._stamp(data, chunk, _draft("MENTOR")) is None
    assert ext._stamp(data, chunk, _draft(None)) is None
    assert ext._stamp(data, chunk, _draft("SPEAKER_99")) is None


def test_two_learners_in_one_chunk_get_distinct_card_ids():
    """The learner is part of the id key, so same-titled cards do not collide."""
    ext = _extractor()
    data = _input(json.dumps(TRANSCRIPT).encode(), {"SPEAKER_01": LEARNER_A, "SPEAKER_02": LEARNER_B})
    chunk = Chunk(anchor="turn:0-3", text="x")

    a = ext._stamp(data, chunk, _draft("SPEAKER_01", title="Same title"))
    b = ext._stamp(data, chunk, _draft("SPEAKER_02", title="Same title"))

    assert a.learner_id == LEARNER_A and b.learner_id == LEARNER_B
    assert a.id != b.id


def test_card_id_is_stable_across_re_extraction():
    ext = _extractor()
    data = _input(json.dumps(TRANSCRIPT).encode(), {"SPEAKER_01": LEARNER_A})
    chunk = Chunk(anchor="turn:0-3", text="x")

    first = ext._stamp(data, chunk, _draft("SPEAKER_01"))
    second = ext._stamp(data, chunk, _draft("SPEAKER_01"))
    assert first.id == second.id


# -------------------------------------------------------------------- agent


class FakeLLM:
    """Returns a queued response per schema type, recording what it was asked."""

    def __init__(self, **by_schema):
        self._by_schema = {k: list(v) for k, v in by_schema.items()}
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def structured(self, messages, *, schema, trace, **_):
        self.calls.append(schema.__name__)
        self.prompts.append(messages[-1]["content"])
        queue = self._by_schema[schema.__name__]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _trace() -> TraceContext:
    return TraceContext(name="extract.meeting_transcript", trace_id="t", tags=["extraction"])


async def _run(agent, chunk="SPEAKER_01: I finished the auth migration.",
               labels=("SPEAKER_01",)):
    return await agent.run(
        chunk=chunk, occurred_at="2026-03-01T00:00:00+00:00",
        context={"participant_labels": list(labels), "participants": ", ".join(labels)},
        trace=_trace(),
    )


async def test_agent_runs_analyze_draft_critique_in_order():
    from learner_memory.extractors.agents._common import Critique

    llm = FakeLLM(
        MeetingAnalysis=[MeetingAnalysis(
            participants=[ParticipantObservation(speaker_label="SPEAKER_01", observations="shipped auth")]
        )],
        ExtractionResult=[ExtractionResult(cards=[_draft("SPEAKER_01", quote="I finished the auth migration.")])],
        Critique=[Critique()],
    )
    cards = await _run(MeetingTranscriptAgent(llm))

    assert llm.calls == ["MeetingAnalysis", "ExtractionResult", "Critique"]
    assert len(cards) == 1
    assert cards[0].subject_label == "SPEAKER_01"


async def test_agent_drops_card_attributed_outside_the_roster():
    """Deterministic attribution backstop: the drafter cannot invent a speaker."""
    from learner_memory.extractors.agents._common import Critique

    llm = FakeLLM(
        MeetingAnalysis=[MeetingAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft("MENTOR", quote="I finished the auth migration.")])],
        Critique=[Critique(verdicts=[{"index": 0, "keep": True}])],
    )
    assert await _run(MeetingTranscriptAgent(llm), labels=("SPEAKER_01",)) == []


async def test_agent_drops_fabricated_quote_even_when_critic_approves():
    from learner_memory.extractors.agents._common import Critique

    llm = FakeLLM(
        MeetingAnalysis=[MeetingAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft("SPEAKER_01", quote="never said this")])],
        Critique=[Critique(verdicts=[{"index": 0, "keep": True}])],
    )
    assert await _run(MeetingTranscriptAgent(llm)) == []


async def test_critique_rejection_drops_the_card():
    from learner_memory.extractors.agents._common import Critique

    llm = FakeLLM(
        MeetingAnalysis=[MeetingAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft("SPEAKER_01", quote="I finished the auth migration.")])],
        Critique=[Critique(verdicts=[{"index": 0, "keep": False, "reason": "misattributed"}])],
    )
    assert await _run(MeetingTranscriptAgent(llm)) == []


# ------------------------------------------------------------------ payload


async def test_extract_chunk_stamps_meeting_provenance():
    ext = _extractor()
    data = _input(json.dumps(TRANSCRIPT).encode(), {"SPEAKER_01": LEARNER_A})
    chunk = Chunk(anchor="turn:0-3", text="SPEAKER_01: I finished the auth migration.")

    from learner_memory.extractors.agents._common import Critique

    llm = FakeLLM(
        MeetingAnalysis=[MeetingAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft("SPEAKER_01", quote="I finished the auth migration.")])],
        Critique=[Critique()],
    )
    ext._agent = MeetingTranscriptAgent(llm)
    drafts = await ext._extract_chunk(data, chunk)

    payload = drafts[0].payload
    assert payload["meeting_type"] == "standup"
    assert payload["title"] == "Daily standup"
    assert payload["subject_label"] == "SPEAKER_01"
    assert payload["turn_range"] == "turn:0-3"
    assert payload["participant_count"] == 1


# --------------------------------------------------------------- chunking


def test_transcript_chunker_anchors_on_turn_ranges():
    text = "\n".join(f"S: line {i}" for i in range(10))
    chunks = TranscriptChunker(turns_per_chunk=4, overlap=1).split(text)

    assert chunks[0].anchor == "turn:0-3"
    assert all(c.anchor.startswith("turn:") for c in chunks)
