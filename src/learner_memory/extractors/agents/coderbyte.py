"""LangGraph agent: Coderbyte assessment -> memory card drafts.

A single LLM call is a poor fit for an assessment. The interesting signal is not
in any one answer but in the *pattern* across them — which topics held up, where
the learner slowed down, whether a wrong answer was a near miss or a blank. So
this runs as a small graph with a self-correction loop:

    analyze ──> draft ──> critique ──┬──(revise, bounded)──> draft
                                     └──(accept)──> finalize

  analyze   one pass over the questions, producing per-topic signals rather than
            per-question trivia. Keeps the drafting step from drowning in text.
  draft     turns those signals into MemoryCardDraft objects.
  critique  an adversarial read: is each card grounded in the source, or inferred?
  finalize  applies the critique, then enforces grounding *deterministically* —
            an `evidence_quote` that is not actually in the source is dropped, no
            matter what the critic said about it.

The graph owns judgement only. Identity, provenance, taxonomy filtering and the
deterministic card id all stay in BaseExtractor._stamp, so an agent can never
spoof them.

LLM access goes through LLMClient (the LiteLLM proxy) rather than a LangChain
chat model, so every node lands in the same Langfuse trace as the rest of the
pipeline.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from learner_memory.core.logging import get_logger
from learner_memory.core.taxonomy import Taxonomy
from learner_memory.extractors.prompt_loader import PromptLibrary
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.memory_card import ExtractionResult, MemoryCardDraft

log = get_logger(__name__)

MAX_REVISIONS = 1
SOURCE_TYPE = "coderbyte_assessment"

# Prompts render under StrictUndefined, so every optional variable a template can
# reference needs a value. Defaulting here (rather than guarding in each template)
# keeps StrictUndefined useful for catching genuine typos, while a caller that
# omits an optional key gets an empty section instead of a failed extraction.
PROMPT_DEFAULTS: dict[str, Any] = {
    "assessment_name": None,
    "role_target": None,
    "analysis": "",
    "guidance": "",
}


# ---------------------------------------------------------------- LLM schemas


class TopicSignal(BaseModel):
    """What the assessment says about one topic, aggregated across questions."""

    topic: str
    outcome: Literal["strong", "mixed", "weak", "not_attempted"]
    evidence: str = Field(description="Which questions support this, and how they went")


class AssessmentAnalysis(BaseModel):
    signals: list[TopicSignal] = Field(default_factory=list)
    overall: str = Field("", description="One paragraph on the shape of the performance")


class CardVerdict(BaseModel):
    index: int = Field(description="Position of the card in the drafted list, zero-based")
    keep: bool
    reason: str = ""


class Critique(BaseModel):
    verdicts: list[CardVerdict] = Field(default_factory=list)
    guidance: str = Field("", description="What to fix if the drafts should be revised")


# ---------------------------------------------------------------- graph state


def _last(_: Any, new: Any) -> Any:
    """Reducer: a node's value replaces the previous one."""
    return new


class AgentState(TypedDict, total=False):
    # inputs (constant for the run)
    chunk: str
    occurred_at: str
    context: dict[str, Any]
    # working values
    analysis: Annotated[AssessmentAnalysis | None, _last]
    drafts: Annotated[list[MemoryCardDraft], _last]
    critique: Annotated[Critique | None, _last]
    guidance: Annotated[str, _last]
    revisions: Annotated[int, _last]


class CoderbyteAgent:
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
            schema=AssessmentAnalysis,
            trace=_sub_trace(state, "analyze"),
        )
        log.info("coderbyte.analyzed", signals=len(analysis.signals))
        return {"analysis": analysis}

    async def _draft(self, state: AgentState) -> dict[str, Any]:
        analysis = state.get("analysis")
        # A critique already in state means the router sent us back: this pass is a
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
            trace=_sub_trace(state, "draft"),
        )
        log.info("coderbyte.drafted", cards=len(result.cards), revision=revisions)
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
                    f"[{i}] {c.title}\n"
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
            trace=_sub_trace(state, "critique"),
        )
        rejected = [v.index for v in critique.verdicts if not v.keep]
        log.info("coderbyte.critiqued", rejected=len(rejected))
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

        kept = [c for c in drafts if _quote_is_grounded(c, state["chunk"])]
        if len(kept) != len(drafts):
            log.info("coderbyte.ungrounded_dropped", count=len(drafts) - len(kept))
        return {"drafts": kept}


# ---------------------------------------------------------------- helpers


def _public(context: dict[str, Any]) -> dict[str, Any]:
    """Prompt variables only — strips the trace object we smuggle through state,
    and fills in defaults for anything a template may reference."""
    return {**PROMPT_DEFAULTS, **{k: v for k, v in context.items() if not k.startswith("_")}}


def _sub_trace(state: AgentState, node: str) -> TraceContext:
    """Each node is its own generation under the extractor's trace."""
    parent: TraceContext = state["context"]["_trace"]
    return TraceContext(
        name=f"{parent.name}.{node}",
        trace_id=parent.trace_id,
        session_id=parent.session_id,
        user_id=parent.user_id,
        tags=[*parent.tags, f"node:{node}"],
        metadata={**parent.metadata, "graph_node": node},
    )


_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _quote_is_grounded(card: MemoryCardDraft, source: str) -> bool:
    """A quote that is not in the source is a fabrication, whatever the critic says.

    Whitespace is normalized because models reflow code and prose freely; anything
    beyond that (paraphrase, ellipsis, invented text) fails.
    """
    if not card.evidence_quote:
        return True
    return _normalize(card.evidence_quote) in _normalize(source)
