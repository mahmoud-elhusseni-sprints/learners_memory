"""Meeting transcript agent — multi-learner.

A meeting holds evidence about several learners at once, and the signal is often
relational — who unblocked whom, who raised the risk, who deferred. The analyze
step summarises each participant in turn; the shared extraction graph (see
agents.graph) does the rest.

The one source-specific rule is attribution: a card names a speaker label, and
`_keep` drops any card whose label is not in the roster the run was given, so the
drafter cannot invent a speaker. Turning a label into a learner id still happens
in BaseExtractor._stamp, never here.
"""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from learner_memory.extractors.agents.graph import GraphExtractionAgent, GraphState
from learner_memory.schemas.memory_card import MemoryCardDraft


class ParticipantObservation(BaseModel):
    """What the segment shows about one participant, drawn from their own turns."""

    speaker_label: str = Field(description="The diarized label, e.g. SPEAKER_01")
    observations: str = Field(description="What this participant demonstrated, with turns cited")


class MeetingAnalysis(BaseModel):
    participants: list[ParticipantObservation] = Field(default_factory=list)
    dynamics: str = Field("", description="How participants engaged with each other")


class MeetingTranscriptAgent(GraphExtractionAgent):
    source_type = "meeting_transcript"
    analysis_schema = MeetingAnalysis
    prompt_defaults: ClassVar[dict[str, Any]] = {
        "meeting_type": None,
        "title": None,
        "participants": "",
        "analysis": "",
        "guidance": "",
    }

    def _keep(self, card: MemoryCardDraft, state: GraphState) -> bool:
        """A card must name a speaker in the roster. An empty roster means the caller
        did not scope attribution, so the base extractor will resolve it instead."""
        roster = set(state["context"].get("participant_labels", []))
        if not roster:
            return True
        return (card.subject_label or "").strip() in roster
