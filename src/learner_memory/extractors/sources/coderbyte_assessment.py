"""Coderbyte assessment extractor.

Flow, end to end:

    learn-os reader service ──JSON──> POST /v1/ingest/coderbyte_assessment
      -> archived in Supabase, RawDocument row, extract.extract_document enqueued
      -> parse()    JSON  -> canonical text, one block per question, no PII
      -> chunker    text  -> question groups (anchor = question range)
      -> agent      chunk -> MemoryCardDraft[]  (LangGraph: analyze/draft/critique)
      -> _stamp()   draft -> MemoryCard         (identity, taxonomy, provenance)
      -> worker     cards -> Postgres ledger, then embedded into Qdrant

Only `parse` and the agent hand-off are source-specific; everything else is the
shared pipeline in BaseExtractor.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from learner_memory.extractors.agents.coderbyte import CoderbyteAgent
from learner_memory.extractors.base import BaseExtractor, ExtractionInput
from learner_memory.extractors.chunking import Chunk, QuestionChunker
from learner_memory.extractors.registry import register
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.coderbyte import (
    CoderbyteAssessment,
    CoderbyteQuestion,
    QuestionKind,
    RubricScore,
)
from learner_memory.schemas.memory_card import MemoryCardDraft, SourceType

QUESTION_MARKER = "### QUESTION"


class CoderbyteAssessmentPayload(BaseModel):
    """Per-card payload: enough to trace a claim back to the assessment."""

    assessment_id: str | None = None
    assessment_name: str | None = None
    question_ids: list[str] = Field(default_factory=list)
    question_kinds: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    score: float | None = None
    max_score: float | None = None
    percentile: float | None = None
    language: str | None = None


@register
class CoderbyteAssessmentExtractor(BaseExtractor):
    source_type = SourceType.CODERBYTE_ASSESSMENT
    version = "coderbyte_assessment@1.0"
    prompt_version = "v1"
    payload_model = CoderbyteAssessmentPayload
    chunker = QuestionChunker(questions_per_chunk=5)

    def __init__(self, llm: LLMClient | None = None, prompts: Any = None,
                 agent: CoderbyteAgent | None = None) -> None:
        super().__init__(llm=llm, prompts=prompts)
        self._agent = agent or CoderbyteAgent(self._llm, self._prompts)
        # parse() -> _extract_chunk() within a single run(); the registry hands out a
        # fresh instance per document, so this never crosses documents.
        self._assessment: CoderbyteAssessment | None = None
        self._by_question: dict[str, CoderbyteQuestion] = {}

    # ------------------------------------------------------------------ parse

    def parse(self, data: ExtractionInput) -> str:
        """JSON -> canonical text.

        Deliberately lossy in one direction: candidate identity is never carried
        through. The learner is already known from the ingest envelope, and card
        content must stay free of identifying details.
        """
        doc = json.loads(data.raw.decode("utf-8", errors="replace"))
        assessment = CoderbyteAssessment.from_document(doc)
        questions = assessment.questions_or_raise()
        self._assessment = assessment
        self._by_question = {q.id or f"idx{i}": q for i, q in enumerate(questions)}

        header = [f"Assessment: {assessment.name or 'Coderbyte assessment'}"]
        if (score := assessment.score_line()) is not None:
            header.append(f"Overall score: {score}")
        if assessment.duration_seconds is not None:
            header.append(f"Total time: {_minutes(assessment.duration_seconds)}")
        header.append(f"Questions: {len(questions)}")

        blocks = [_render_question(i, q) for i, q in enumerate(questions)]
        return "\n".join(header) + "\n\n" + "\n\n".join(blocks)

    # ------------------------------------------------------------- agent hook

    async def _extract_chunk(self, data: ExtractionInput, chunk: Chunk) -> list[MemoryCardDraft]:
        """Replaces the base class's single LLM call with the LangGraph agent."""
        trace = TraceContext(
            name=f"extract.{self.source_type}",
            trace_id=str(data.document_id),
            session_id=str(data.document_id),
            user_id=str(data.learner_id),
            tags=["extraction", str(self.source_type), "langgraph"],
            metadata={
                "organization_id": str(data.organization_id),
                "extractor_version": self.version,
                "prompt_version": self.prompt_version,
                "chunk_anchor": chunk.anchor,
            },
        )
        drafts = await self._agent.run(
            chunk=chunk.text,
            occurred_at=data.occurred_at.isoformat(),
            context=self.build_context(data, chunk),
            trace=trace,
        )
        for draft in drafts:
            draft.payload = self._payload_for(chunk) | (draft.payload or {})
        return drafts

    def _payload_for(self, chunk: Chunk) -> dict[str, Any]:
        """Provenance is stamped from the parsed document, not asked of the model.

        The chunk anchor already names the questions the cards came from, so the
        payload can point back at them without trusting the LLM to copy ids.
        """
        a = self._assessment
        ids = [i for i in chunk.anchor.removeprefix("questions:").split(",") if i]
        seen = [self._by_question[i] for i in ids if i in self._by_question]
        languages = {q.language for q in seen if q.language}
        return CoderbyteAssessmentPayload(
            assessment_id=a.assessment_id if a else None,
            assessment_name=a.name if a else None,
            question_ids=ids,
            question_kinds=sorted({q.normalized_kind.value for q in seen}),
            topics=sorted({t for q in seen for t in q.topics}),
            score=a.score if a else None,
            max_score=a.max_score if a else None,
            percentile=a.percentile if a else None,
            language=languages.pop() if len(languages) == 1 else None,
        ).model_dump(mode="json")

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        m = data.metadata
        return {
            "assessment_name": m.get("assessment_name") or m.get("title"),
            "role_target": m.get("role_target"),
        }


# ---------------------------------------------------------------- rendering


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min"


def _render_question(index: int, q: CoderbyteQuestion) -> str:
    """One question as a self-contained, greppable text block.

    The marker line carries the id so a chunk anchor — and therefore a card id —
    stays stable across re-extractions even if the reader service reorders the list.
    """
    ident = q.id or f"idx{index}"
    lines = [f"{QUESTION_MARKER} {ident}"]

    if q.title:
        lines.append(f"Title: {q.title}")
    kind = q.normalized_kind
    if kind is not QuestionKind.UNKNOWN:
        lines.append(f"Type: {kind.value}")
    elif q.kind:
        lines.append(f"Type: {q.kind}")
    if q.language:
        lines.append(f"Language: {q.language}")
    if q.topics:
        lines.append(f"Topics: {', '.join(q.topics)}")

    outcome = []
    if q.is_correct is not None:
        outcome.append("correct" if q.is_correct else "incorrect")
    if (line := q.score_line()) is not None:
        outcome.append(f"score {line}")
    if q.test_cases and (summary := q.test_cases.summary):
        outcome.append(summary)
    if q.attempts is not None:
        outcome.append(f"{q.attempts} attempt(s)")
    if q.time_spent_seconds is not None:
        outcome.append(f"{q.time_spent_seconds:.0f}s spent")
    if outcome:
        lines.append(f"Outcome: {', '.join(outcome)}")

    if q.prompt:
        lines.append(f"Prompt:\n{q.prompt.strip()}")

    lines.extend(_render_body(q))
    return "\n".join(lines)


def _render_body(q: CoderbyteQuestion) -> list[str]:
    """The answer, rendered on the terms of the question that was asked.

    The three shapes are not interchangeable evidence: a picked option shows
    recognition, submitted code shows production, prose shows reasoning. Each
    needs different surrounding context to be readable — an MCQ answer without
    the alternatives is meaningless, and an essay without the grader's rubric is
    just unscored text.
    """
    kind = q.normalized_kind

    if kind is QuestionKind.MULTIPLE_CHOICE and q.options:
        return _render_choice(q)

    lines: list[str] = []
    label = "Learner's code" if kind is QuestionKind.CODING else "Learner's answer"
    lines.append(f"{label}:\n{q.answer.strip() if q.answer else '(no answer submitted)'}")
    if q.expected_answer:
        lines.append(f"Expected answer:\n{q.expected_answer.strip()}")
    if q.rubric:
        lines.append("Rubric: " + "; ".join(_rubric_line(r) for r in q.rubric))
    if q.grader_feedback:
        lines.append(f"Grader feedback:\n{q.grader_feedback.strip()}")
    return lines


def _render_choice(q: CoderbyteQuestion) -> list[str]:
    """Options with the learner's pick and the correct one marked inline.

    Which distractor was chosen is the whole signal on a wrong MCQ — it separates
    a plausible misconception from a random guess — so the alternatives have to be
    in the text, not just the answer.
    """
    chosen = {id(o) for o in q.chosen_options()}
    lines = ["Options offered:"]
    for i, opt in enumerate(q.options):
        marks = []
        if id(opt) in chosen:
            marks.append("chosen by learner")
        if opt.is_correct:
            marks.append("correct")
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        lines.append(f"  [{opt.id or chr(65 + i)}] {(opt.text or '').strip()}{suffix}")

    if not chosen:
        # The source never said which option was picked; fall back to the raw answer.
        lines.append(f"Learner's answer:\n{q.answer.strip() if q.answer else '(no answer submitted)'}")
    if q.expected_answer and not any(o.is_correct for o in q.options):
        lines.append(f"Expected answer:\n{q.expected_answer.strip()}")
    if q.grader_feedback:
        lines.append(f"Grader feedback:\n{q.grader_feedback.strip()}")
    return lines


def _rubric_line(r: RubricScore) -> str:
    name = r.criterion or "criterion"
    if r.score is None:
        return f"{name} (ungraded)"
    score = f"{r.score:g}/{r.max_score:g}" if r.max_score is not None else f"{r.score:g}"
    return f"{name} {score}" + (f" ({r.comment.strip()})" if r.comment else "")
