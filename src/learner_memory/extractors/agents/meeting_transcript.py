"""LangGraph agent: a diarized meeting segment -> memory card drafts, per learner.

A meeting is multi-subject in a way an assessment is not: the same transcript
holds evidence about several learners at once, and the signal is often *relational*
— who unblocked whom, who raised the risk, who deferred. A single pass would blur
those together, so this runs the same self-correcting graph as the assessment
agent but analyses each participant in turn:

    analyze ──> draft ──> critique ──┬──(revise, bounded)──> draft
                                     └──(accept)──> finalize

  analyze   one pass over the segment, producing per-participant observations from
            each learner's own turns and how they engaged with others.
  draft     turns those into MemoryCardDraft objects, each tagged with the speaker
            label it is about (`subject_label`).
  critique  an adversarial read: is each card grounded, and attributed to the right
            speaker rather than to something another participant said?
  finalize  applies the critique, then enforces two rules deterministically — the
            `evidence_quote` must be in the source, and the `subject_label` must be
            a known participant. Neither the drafter nor the critic can override
            these; a card that fails is dropped.

Only judgement lives here. Turning a speaker label into a learner id, and the
deterministic card id, stay in BaseExtractor._stamp, so the agent can never
attribute a card to a learner outside the roster it was given.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from learner_memory.core.logging import get_logger
from learner_memory.core.taxonomy import Taxonomy
from learner_memory.extractors.agents._common import (
    Critique,
    public_context,
    quote_is_grounded,
    sub_trace,
)
from learner_memory.extractors.agents._common import (
    last as _last,
)
from learner_memory.extractors.prompt_loader import PromptLibrary
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.memory_card import ExtractionResult, MemoryCardDraft

log = get_logger(__name__)

MAX_REVISIONS = 1
SOURCE_TYPE = "meeting_transcript"

# Optional template variables get a default so StrictUndefined still catches typos.
PROMPT_DEFAULTS: dict[str, Any] = {
    "meeting_type": None,
    "title": None,
    "participants": "",
    "analysis": "",
    "guidance": "",
}


# ---------------------------------------------------------------- LLM schemas


class ParticipantObservation(BaseModel):
    """What the segment shows about one participant, drawn from their own turns."""

    speaker_label: str = Field(description="The diarized label, e.g. SPEAKER_01")
    observations: str = Field(description="What this participant demonstrated, with turns cited")


class MeetingAnalysis(BaseModel):
    participants: list[ParticipantObservation] = Field(default_factory=list)
    dynamics: str = Field("", description="How participants engaged with each other")


# ---------------------------------------------------------------- graph state


class AgentState(TypedDict, total=False):
    # inputs (constant for the run)
    chunk: str
    occurred_at: str
    context: dict[str, Any]
    # working values
    analysis: Annotated[MeetingAnalysis | None, _last]
    drafts: Annotated[list[MemoryCardDraft], _last]
    critique: Annotated[Critique | None, _last]
    guidance: Annotated[str, _last]
    revisions: Annotated[int, _last]


class MeetingTranscriptAgent:
    """Compiled once per extractor instance; `run` is safe to call concurrently."""

    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptLibrary | None = None,
        *,
        max_revisions: int = MAX_REVISIONS,
    ) -> None:
        self._llm = llm
        self._prompts = prompts or PromptLibrary()
        self._max_revisions = max_revisions
        self._graph = self._build().compile()

    # ------------------------------------------------------------ public API

    async def run(
        self,
        *,
        chunk: str,
        occurred_at: str,
        context: dict[str, Any],
        trace: TraceContext,
    ) -> list[MemoryCardDraft]:
        state: AgentState = {
            "chunk": chunk,
            "occurred_at": occurred_at,
            "context": {**context, "_trace": trace},
            "drafts": [],
            "guidance": "",
            "revisions": 0,
        }
        final = await self._graph.ainvoke(state)
        return final.get("drafts", [])

    # ---------------------------------------------------------------- graph

    def _build(self) -> StateGraph:
        g = StateGraph(AgentState)
        g.add_node("analyze", self._analyze)
        g.add_node("draft", self._draft)
        g.add_node("critique", self._critique)
        g.add_node("finalize", self._finalize)

        g.set_entry_point("analyze")
        g.add_edge("analyze", "draft")
        g.add_edge("draft", "critique")
        g.add_conditional_edges(
            "critique", self._route, {"revise": "draft", "accept": "finalize"}
        )
        g.add_edge("finalize", END)
        return g

    # ---------------------------------------------------------------- nodes

    async def _analyze(self, state: AgentState) -> dict[str, Any]:
        rendered = self._prompts.render(
            source_type=SOURCE_TYPE,
            version="v1.analyze",
            context={
                **_public(state["context"]),
                "chunk": state["chunk"],
                "occurred_at": state["occurred_at"],
            },
        )
        analysis = await self._llm.structured(
            [{"role": "system", "content": rendered.system},
             {"role": "user", "content": rendered.user}],
            schema=MeetingAnalysis,
            trace=sub_trace(state["context"]["_trace"], "analyze"),
        )
        log.info("meeting.analyzed", participants=len(analysis.participants))
        return {"analysis": analysis}

    async def _draft(self, state: AgentState) -> dict[str, Any]:
        analysis = state.get("analysis")
        # A critique already in state means the router sent us back: this is a
        # revision, and the critic's guidance is the brief for it.
        previous = state.get("critique")
        revisions = state.get("revisions", 0) + (1 if previous else 0)
        guidance = previous.guidance if previous else ""

        rendered = self._prompts.render(
            source_type=SOURCE_TYPE,
            version="v1",
            context={
                **_public(state["context"]),
                "chunk": state["chunk"],
                "occurred_at": state["occurred_at"],
                "taxonomy": Taxonomy.prompt_block(),
                "analysis": analysis.model_dump_json(indent=2) if analysis else "",
                "guidance": guidance,
            },
        )
        result = await self._llm.structured(
            [{"role": "system", "content": rendered.system},
             {"role": "user", "content": rendered.user}],
            schema=ExtractionResult,
            trace=sub_trace(state["context"]["_trace"], "draft"),
        )
        log.info("meeting.drafted", cards=len(result.cards), revision=revisions)
        return {"drafts": result.cards, "revisions": revisions, "guidance": guidance}

    async def _critique(self, state: AgentState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        if not drafts:
            # Nothing to argue about; an empty extraction is a valid answer.
            return {"critique": Critique()}

        rendered = self._prompts.render(
            source_type=SOURCE_TYPE,
            version="v1.critique",
            context={
                **_public(state["context"]),
                "chunk": state["chunk"],
                "occurred_at": state["occurred_at"],
                "cards": "\n".join(
                    f"[{i}] ({c.subject_label or 'UNASSIGNED'}) {c.title}\n"
                    f"    content: {c.content}\n"
                    f"    quote: {c.evidence_quote or '(none)'}\n"
                    f"    contributions: {[f'{x.target}:{x.key}@L{x.level_signal}' for x in c.contributions]}"
                    for i, c in enumerate(drafts)
                ),
            },
        )
        critique = await self._llm.structured(
            [{"role": "system", "content": rendered.system},
             {"role": "user", "content": rendered.user}],
            schema=Critique,
            trace=sub_trace(state["context"]["_trace"], "critique"),
        )
        rejected = [v.index for v in critique.verdicts if not v.keep]
        log.info("meeting.critiqued", rejected=len(rejected))
        return {"critique": critique}

    def _route(self, state: AgentState) -> str:
        critique = state.get("critique")
        if critique is None or not critique.verdicts:
            return "accept"
        rejected = [v for v in critique.verdicts if not v.keep]
        if rejected and state.get("revisions", 0) < self._max_revisions:
            return "revise"
        return "accept"

    async def _finalize(self, state: AgentState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        critique = state.get("critique")

        if critique and critique.verdicts:
            dropped = {v.index for v in critique.verdicts if not v.keep}
            drafts = [c for i, c in enumerate(drafts) if i not in dropped]

        roster = set(state["context"].get("participant_labels", []))
        kept = [
            c for c in drafts
            if quote_is_grounded(c, state["chunk"]) and _attributed(c, roster)
        ]
        if len(kept) != len(drafts):
            log.info("meeting.dropped", count=len(drafts) - len(kept))
        return {"drafts": kept}


# ---------------------------------------------------------------- helpers


def _public(context: dict[str, Any]) -> dict[str, Any]:
    return public_context(context, PROMPT_DEFAULTS)


def _attributed(card: MemoryCardDraft, roster: set[str]) -> bool:
    """A card must name a speaker in the roster. An empty roster means the caller
    did not scope attribution, so the base extractor will resolve it instead."""
    if not roster:
        return True
    return (card.subject_label or "").strip() in roster
