"""Coderbyte assessment: JSON parsing, chunking, and the LangGraph agent.

The agent is driven by a fake LLM so the graph's control flow — the critique
loop, the revision bound, the grounding check — is tested without a network call.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from learner_memory.extractors.agents.coderbyte import (
    AssessmentAnalysis,
    CoderbyteAgent,
    Critique,
)
from learner_memory.extractors.base import ExtractionInput
from learner_memory.extractors.chunking import QuestionChunker
from learner_memory.extractors.registry import get_extractor, load_extractors
from learner_memory.llm.client import TraceContext
from learner_memory.schemas.coderbyte import CoderbyteAssessment
from learner_memory.schemas.memory_card import ExtractionResult, MemoryCardDraft, SourceType

ASSESSMENT = {
    "assessment": {
        "id": "cb-991",
        "name": "Backend Engineer Screen",
        "score": 72,
        "max_score": 100,
        "percentile": 64,
        "duration_seconds": 2700,
    },
    "candidate": {"name": "Real Person", "email": "real.person@example.com"},
    "questions": [
        {
            "id": "q1",
            "type": "coding",
            "title": "Two Sum",
            "language": "python",
            "topics": ["algorithms", "hash maps"],
            "prompt": "Return indices of two numbers adding to target.",
            "answer": "def two_sum(nums, t):\n    seen = {}\n    for i, n in enumerate(nums):\n        if t - n in seen:\n            return [seen[t - n], i]\n        seen[n] = i",
            "correct": True,
            "score": 10,
            "max_score": 10,
            "test_cases": {"passed": 8, "total": 8},
            "time_spent_seconds": 240,
        },
        {
            "id": "q2",
            "type": "multiple_choice",
            "title": "SQL index behaviour",
            "topics": ["databases"],
            "prompt": "Which index helps this query?",
            "answer": "A composite index on (a)",
            "expected_answer": "A composite index on (a, b)",
            "correct": False,
            "score": 0,
            "max_score": 5,
            "time_spent_seconds": 20,
        },
    ],
}


def _input(raw: bytes) -> ExtractionInput:
    return ExtractionInput(
        document_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        learner_id=uuid.uuid4(),
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
        raw=raw,
    )


# ------------------------------------------------------------------ parsing


def test_parse_renders_questions_answers_and_outcomes():
    load_extractors()
    text = get_extractor(SourceType.CODERBYTE_ASSESSMENT).parse(_input(json.dumps(ASSESSMENT).encode()))

    assert "Backend Engineer Screen" in text
    assert "72/100 (64th percentile)" in text
    assert "### QUESTION q1" in text and "### QUESTION q2" in text
    assert "def two_sum" in text                      # the learner's actual work
    assert "8/8 test cases passed" in text
    assert "A composite index on (a, b)" in text      # expected answer, for contrast


def test_parse_drops_candidate_identity():
    """PII must not reach the prompt: the learner is already known from ingest."""
    load_extractors()
    text = get_extractor(SourceType.CODERBYTE_ASSESSMENT).parse(_input(json.dumps(ASSESSMENT).encode()))

    assert "Real Person" not in text
    assert "real.person@example.com" not in text


def test_parse_accepts_alternative_field_names():
    """The reader service is outside our release cycle; aliases keep it working."""
    doc = {
        "title": "Screen",
        "items": [{"question_id": "x1", "name": "Q", "response": "answer text", "passed": True}],
    }
    parsed = CoderbyteAssessment.from_document(doc)
    assert parsed.name == "Screen"
    assert parsed.questions[0].id == "x1"
    assert parsed.questions[0].answer == "answer text"
    assert parsed.questions[0].is_correct is True


def test_empty_question_list_fails_loudly():
    with pytest.raises(ValueError, match="no questions"):
        CoderbyteAssessment.from_document({"name": "Empty"}).questions_or_raise()


# ----------------------------------------------------------------- chunking


def test_question_chunker_keeps_questions_whole_and_anchors_on_ids():
    text = "Assessment: X\nOverall score: 1/2\n\n### QUESTION q1\nbody one\n\n### QUESTION q2\nbody two"
    chunks = QuestionChunker(questions_per_chunk=1).split(text)

    assert [c.anchor for c in chunks] == ["questions:q1", "questions:q2"]
    assert "body one" in chunks[0].text and "body two" not in chunks[0].text
    # Every group carries the preamble, so a chunk can read its own score context.
    assert "Overall score: 1/2" in chunks[1].text


def test_question_chunker_anchor_is_stable_under_reordering():
    a = "### QUESTION q1\none\n\n### QUESTION q2\ntwo"
    b = "### QUESTION q2\ntwo\n\n### QUESTION q1\none"
    chunker = QuestionChunker(questions_per_chunk=1)

    assert {c.anchor for c in chunker.split(a)} == {c.anchor for c in chunker.split(b)}


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


def _draft(title="Uses hash maps to trade space for time", quote="seen = {}"):
    return MemoryCardDraft(
        card_type="skill_evidence", title=title,
        content="Reached for a hash map to avoid a nested scan.",
        evidence_quote=quote,
    )


def _agent(llm) -> CoderbyteAgent:
    return CoderbyteAgent(llm)


def _trace() -> TraceContext:
    return TraceContext(name="extract.coderbyte_assessment", trace_id="t", tags=["extraction"])


async def _run(agent, chunk="seen = {}\nfor i, n in enumerate(nums):"):
    return await agent.run(
        chunk=chunk, occurred_at="2026-03-01T00:00:00+00:00", context={}, trace=_trace()
    )


async def test_agent_runs_analyze_draft_critique_in_order():
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis(overall="solid on algorithms")],
        ExtractionResult=[ExtractionResult(cards=[_draft()])],
        Critique=[Critique()],
    )
    cards = await _run(_agent(llm))

    assert llm.calls == ["AssessmentAnalysis", "ExtractionResult", "Critique"]
    assert len(cards) == 1


async def test_critique_rejection_drops_the_card():
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft()])],
        Critique=[Critique(verdicts=[{"index": 0, "keep": False, "reason": "one question"}])],
    )
    assert await _run(_agent(llm)) == []


async def test_rejection_triggers_one_revision_then_stops():
    """The loop must be bounded — a stubborn critic cannot spin forever."""
    rejected = Critique(verdicts=[{"index": 0, "keep": False, "reason": "ungrounded"}],
                        guidance="quote the source verbatim")
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft()]), ExtractionResult(cards=[_draft()])],
        Critique=[rejected, rejected],
    )
    cards = await _run(_agent(llm))

    assert llm.calls.count("ExtractionResult") == 2      # drafted, revised, then gave up
    assert llm.calls.count("Critique") == 2
    assert cards == []


async def test_revision_passes_the_critics_guidance_back_into_the_prompt():
    rejected = Critique(verdicts=[{"index": 0, "keep": False, "reason": "no"}],
                        guidance="cite the failing test case")
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft()])],
        Critique=[rejected],
    )
    await _run(_agent(llm))

    redraft = [p for p in llm.prompts if "cite the failing test case" in p]
    assert redraft, "the critic's guidance never reached the redraft prompt"


async def test_fabricated_quote_is_dropped_even_when_the_critic_approves():
    """Deterministic grounding backstop: the critic is not the last word."""
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft(quote="text that is not in the source")])],
        Critique=[Critique(verdicts=[{"index": 0, "keep": True}])],
    )
    assert await _run(_agent(llm)) == []


async def test_quote_matching_tolerates_reflowed_whitespace():
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft(quote="seen  =  {}")])],
        Critique=[Critique()],
    )
    assert len(await _run(_agent(llm))) == 1


async def test_card_without_a_quote_survives():
    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft(quote=None)])],
        Critique=[Critique()],
    )
    assert len(await _run(_agent(llm))) == 1


# ------------------------------------------------------------------ payload


async def test_payload_provenance_is_stamped_not_asked_of_the_model():
    """Ids come from the parsed document, so a model cannot get them wrong."""
    load_extractors()
    extractor = get_extractor(SourceType.CODERBYTE_ASSESSMENT)
    data = _input(json.dumps(ASSESSMENT).encode())
    text = extractor.parse(data)
    chunk = QuestionChunker(questions_per_chunk=5).split(text)[0]

    llm = FakeLLM(
        AssessmentAnalysis=[AssessmentAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft(quote=None)])],
        Critique=[Critique()],
    )
    extractor._agent = CoderbyteAgent(llm)
    drafts = await extractor._extract_chunk(data, chunk)

    payload = drafts[0].payload
    assert payload["assessment_id"] == "cb-991"
    assert payload["assessment_name"] == "Backend Engineer Screen"
    assert payload["question_ids"] == ["q1", "q2"]
    assert payload["topics"] == ["algorithms", "databases", "hash maps"]
    assert payload["score"] == 72 and payload["max_score"] == 100
    # Only q1 names a language, so the group is unambiguous.
    assert payload["language"] == "python"
