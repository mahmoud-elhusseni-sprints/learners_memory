"""Task review extractor. A complete example of what a new source costs: one file."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from learner_memory.extractors.base import BaseExtractor, ExtractionInput
from learner_memory.extractors.chunking import Chunk, WholeDocumentChunker
from learner_memory.extractors.registry import register
from learner_memory.schemas.memory_card import SourceType


class TaskReviewPayload(BaseModel):
    task_id: str | None = None
    reviewer_id: str | None = None
    rubric_scores: dict[str, float] = Field(default_factory=dict)
    verdict: str | None = None
    iteration: int | None = None
    repo_url: str | None = None


@register
class TaskReviewExtractor(BaseExtractor):
    source_type = SourceType.TASK_REVIEW
    version = "task_review@1.0"
    prompt_version = "v1"
    payload_model = TaskReviewPayload
    chunker = WholeDocumentChunker()

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        m = data.metadata
        return {
            "task_title": m.get("task_title"),
            "iteration": m.get("iteration"),
            "verdict": m.get("verdict"),
        }
