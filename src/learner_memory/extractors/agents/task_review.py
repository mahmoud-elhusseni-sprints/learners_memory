"""Task-review agent.

A single reviewed submission still rewards a pattern read rather than a one-shot
summary: which dimensions the work demonstrated (structure, testing, communication,
follow-through across iterations) and where it fell short. The shared extraction
graph (see agents.graph) does the work; this module supplies only the review-shaped
analysis, and its prompts live under prompts/task_review/. There is no score or
verdict in this source — only the grader's written report.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from learner_memory.extractors.agents.graph import GraphExtractionAgent


class ReviewSignal(BaseModel):
    """What the submission and its grading say about one dimension of the work."""

    dimension: str = Field(description="e.g. code structure, testing, communication")
    outcome: Literal["strong", "mixed", "weak"]
    evidence: str = Field(description="What in the submission or feedback supports this")


class TaskReviewAnalysis(BaseModel):
    signals: list[ReviewSignal] = Field(default_factory=list)
    overall: str = Field("", description="One paragraph on the shape of the submission")


class TaskReviewAgent(GraphExtractionAgent):
    source_type = "task_review"
    analysis_schema = TaskReviewAnalysis
    prompt_defaults: ClassVar[dict[str, Any]] = {
        "task_headline": None,
        "iteration": None,
        "analysis": "",
        "guidance": "",
    }
