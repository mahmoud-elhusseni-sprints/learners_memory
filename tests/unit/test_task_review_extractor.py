"""Task review: envelope parsing, canonical rendering, and the shared graph agent.

The agent is the generic GraphExtractionAgent configured for task_review, so these
tests focus on what is source-specific — parsing the submission/verdict envelope,
rendering it without PII, and stamping provenance — plus a check that the shared
graph still runs analyze -> draft -> critique for this source.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from learner_memory.extractors.agents._common import Critique
from learner_memory.extractors.agents.task_review import TaskReviewAgent, TaskReviewAnalysis
from learner_memory.extractors.base import ExtractionInput
from learner_memory.extractors.chunking import Chunk
from learner_memory.extractors.registry import get_extractor, load_extractors
from learner_memory.llm.client import TraceContext
from learner_memory.schemas.memory_card import ExtractionResult, MemoryCardDraft, SourceType
from learner_memory.schemas.task_review import TaskReview

REVIEW = {
    "task": {
        "title": "Build a rate limiter",
        "task_number": 42,
        "workstream": "backend",
        "description": "Implement a token-bucket rate limiter with tests.",
        "technologies": ["concurrency", "api design"],
    },
    "submission": ["https://git.example/pr/9"],
    "review": {
        "report": "Approach is sound but there are no tests for the refill path.",
        "reviewer_id": "rev-7",
        "iteration": 2,
    },
}


def _input(raw: bytes) -> ExtractionInput:
    return ExtractionInput(
        document_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        learner_id=uuid.uuid4(),
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
        raw=raw,
        metadata={},
    )


def _extractor():
    load_extractors()
    return get_extractor(SourceType.TASK_REVIEW)


# ------------------------------------------------------------------ parsing


def test_from_document_flattens_nested_sections():
    r = TaskReview.from_document(REVIEW)
    assert r.headline == "Build a rate limiter"
    assert r.task_number == 42
    assert r.workstream == "backend"
    assert r.submission == ["https://git.example/pr/9"]
    assert r.report == "Approach is sound but there are no tests for the refill path."
    assert r.reviewer_id == "rev-7"
    assert r.iteration == 2


def test_from_document_accepts_a_flat_envelope():
    flat = {"title": "T", "submission": "https://git.example/pr/1", "report": "solid"}
    r = TaskReview.from_document(flat)
    assert r.headline == "T" and r.submission == ["https://git.example/pr/1"]
    assert r.report == "solid"


def test_parse_renders_task_and_submission_and_report():
    text = _extractor().parse(_input(json.dumps(REVIEW).encode()))

    assert "Task: Build a rate limiter" in text
    assert "Workstream: backend" in text
    assert "Technologies: concurrency, api design" in text
    assert "Learner's submission URLs:\n- https://git.example/pr/9" in text
    assert "Grader's report:\nApproach is sound" in text


def test_empty_submission_fails_loudly():
    with pytest.raises(ValueError, match="no submission"):
        TaskReview.from_document({"title": "T", "report": "ok"}).submission_or_raise()


def test_missing_report_fails_loudly():
    doc = {"title": "T", "submission": ["https://git.example/pr/9"], "grade": "ok"}
    with pytest.raises(ValueError, match="no grader report"):
        TaskReview.from_document(doc).report_or_raise()


def test_blank_report_fails_loudly():
    doc = {"title": "T", "submission": ["https://git.example/pr/9"], "report": "   "}
    with pytest.raises(ValueError, match="no grader report"):
        TaskReview.from_document(doc).report_or_raise()


def test_parse_rejects_a_document_with_no_report():
    doc = {"title": "T", "submission": ["https://git.example/pr/9"]}
    with pytest.raises(ValueError, match="no grader report"):
        _extractor().parse(_input(json.dumps(doc).encode()))


# ------------------------------------------------------------------ agent


class FakeLLM:
    def __init__(self, **by_schema):
        self._by_schema = {k: list(v) for k, v in by_schema.items()}
        self.calls: list[str] = []

    async def structured(self, messages, *, schema, trace, **_):
        self.calls.append(schema.__name__)
        queue = self._by_schema[schema.__name__]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _draft(quote="class RateLimiter:"):
    return MemoryCardDraft(
        card_type="skill_evidence", title="Ships a working structure but skips tests",
        content="Delivered a sound approach yet omitted tests for the refill path.",
        evidence_quote=quote,
    )


async def test_agent_runs_analyze_draft_critique_for_task_review():
    llm = FakeLLM(
        TaskReviewAnalysis=[TaskReviewAnalysis(overall="sound approach, thin tests")],
        ExtractionResult=[ExtractionResult(cards=[_draft()])],
        Critique=[Critique()],
    )
    agent = TaskReviewAgent(llm)
    cards = await agent.run(
        chunk="class RateLimiter:\n    def allow(self): return True",
        occurred_at="2026-03-01T00:00:00+00:00", context={}, trace=_trace(),
    )
    assert llm.calls == ["TaskReviewAnalysis", "ExtractionResult", "Critique"]
    assert len(cards) == 1


async def test_fabricated_quote_is_dropped_even_when_the_critic_approves():
    llm = FakeLLM(
        TaskReviewAnalysis=[TaskReviewAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft(quote="not in the submission")])],
        Critique=[Critique(verdicts=[{"index": 0, "keep": True}])],
    )
    agent = TaskReviewAgent(llm)
    cards = await agent.run(
        chunk="class RateLimiter:", occurred_at="2026-03-01T00:00:00+00:00",
        context={}, trace=_trace(),
    )
    assert cards == []


def _trace() -> TraceContext:
    return TraceContext(name="extract.task_review", trace_id="t", tags=["extraction"])


# ------------------------------------------------------------------ payload


async def test_extract_chunk_stamps_task_provenance():
    ext = _extractor()
    data = _input(json.dumps(REVIEW).encode())
    ext.parse(data)  # populates the parsed review used by the payload
    chunk = Chunk(anchor="doc:0", text="class RateLimiter:")

    llm = FakeLLM(
        TaskReviewAnalysis=[TaskReviewAnalysis()],
        ExtractionResult=[ExtractionResult(cards=[_draft(quote=None)])],
        Critique=[Critique()],
    )
    ext._agent = TaskReviewAgent(llm)
    drafts = await ext._extract_chunk(data, chunk)

    payload = drafts[0].payload
    assert payload["task_headline"] == "Build a rate limiter"
    assert payload["task_number"] == 42
    assert payload["workstream"] == "backend"
    assert payload["technologies"] == ["concurrency", "api design"]
    assert payload["reviewer_id"] == "rev-7"
    assert payload["iteration"] == 2
