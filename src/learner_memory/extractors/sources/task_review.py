"""Task review extractor.

A learner submits work for a task; a grader writes a report on it. That whole
document — task, submission (a list of URLs), report — is one unit of evidence, so
it parses to a single canonical block and runs through the shared extraction graph
in one pass. Single learner: the owner comes from the ingest envelope, so no
attribution is needed.

    ingest ── JSON ──> POST /v1/ingest/task_review
      -> parse()    JSON -> canonical text (task, submission urls, report), no PII
      -> chunker    whole document -> one chunk
      -> agent      chunk -> MemoryCardDraft[]  (analyze/draft/critique/finalize)
      -> _stamp()   draft -> MemoryCard (identity, taxonomy, provenance)
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from learner_memory.extractors.agents.task_review import TaskReviewAgent
from learner_memory.extractors.base import BaseExtractor, ExtractionInput
from learner_memory.extractors.chunking import Chunk, WholeDocumentChunker
from learner_memory.extractors.registry import register
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.memory_card import MemoryCardDraft, SourceType
from learner_memory.schemas.task_review import TaskReview


class TaskReviewPayload(BaseModel):
    """Per-card payload: enough to trace a claim back to the task and its review."""

    task_headline: str | None = None
    task_number: int | None = None
    workstream: str | None = None
    technologies: list[str] = Field(default_factory=list)
    reviewer_id: str | None = None
    iteration: int | None = None


@register
class TaskReviewExtractor(BaseExtractor):
    source_type = SourceType.TASK_REVIEW
    version = "task_review@1.0"
    prompt_version = "v1"
    payload_model = TaskReviewPayload
    chunker = WholeDocumentChunker()

    def __init__(self, llm: LLMClient | None = None, prompts: Any = None,
                 agent: TaskReviewAgent | None = None) -> None:
        super().__init__(llm=llm, prompts=prompts)
        self._agent = agent or TaskReviewAgent(self._llm, self._prompts)
        # parse() -> _extract_chunk() within one run(); the registry hands out a fresh
        # instance per document, so this never crosses documents.
        self._review: TaskReview | None = None

    # ------------------------------------------------------------------ parse

    def parse(self, data: ExtractionInput) -> str:
        doc = json.loads(data.raw.decode("utf-8", errors="replace"))
        review = TaskReview.from_document(doc)
        review.submission_or_raise()
        self._review = review
        return review.to_text()

    # ------------------------------------------------------------- agent hook

    async def _extract_chunk(self, data: ExtractionInput, chunk: Chunk) -> list[MemoryCardDraft]:
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
        payload = self._payload()
        for draft in drafts:
            draft.payload = payload | (draft.payload or {})
        return drafts

    def _payload(self) -> dict[str, Any]:
        """Provenance is stamped from the parsed document, not asked of the model."""
        r = self._review
        return TaskReviewPayload(
            task_headline=r.headline if r else None,
            task_number=r.task_number if r else None,
            workstream=r.workstream if r else None,
            technologies=r.technologies if r else [],
            reviewer_id=r.reviewer_id if r else None,
            iteration=r.iteration if r else None,
        ).model_dump(mode="json")

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        r = self._review
        return {
            "task_headline": (r.headline if r else None) or data.metadata.get("task_title"),
            "iteration": r.iteration if r else data.metadata.get("iteration"),
        }
