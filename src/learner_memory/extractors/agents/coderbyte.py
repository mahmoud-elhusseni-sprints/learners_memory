"""Coderbyte assessment agent.

The interesting signal in an assessment is not any one answer but the *pattern*
across them — which topics held up, where the learner slowed down, whether a wrong
answer was a near miss or a blank. The shared extraction graph (see agents.graph)
handles that pattern; this module supplies only the assessment-shaped analysis and
its prompts live under prompts/coderbyte_assessment/.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

# Re-exported for callers/tests that import the critique schema from this module.
from learner_memory.extractors.agents._common import Critique  # noqa: F401
from learner_memory.extractors.agents.graph import GraphExtractionAgent


class TopicSignal(BaseModel):
    """What the assessment says about one topic, aggregated across questions."""

    topic: str
    outcome: Literal["strong", "mixed", "weak", "not_attempted"]
    evidence: str = Field(description="Which questions support this, and how they went")


class AssessmentAnalysis(BaseModel):
    signals: list[TopicSignal] = Field(default_factory=list)
    overall: str = Field("", description="One paragraph on the shape of the performance")


class CoderbyteAgent(GraphExtractionAgent):
    source_type = "coderbyte_assessment"
    analysis_schema = AssessmentAnalysis
    # Prompts render under StrictUndefined, so every optional variable a template can
    # reference needs a value — an omitted key becomes an empty section, not a crash.
    prompt_defaults: ClassVar[dict[str, Any]] = {
        "assessment_name": None,
        "role_target": None,
        "analysis": "",
        "guidance": "",
    }
