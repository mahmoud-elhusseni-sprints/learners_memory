"""The extraction graph, once.

Every source that needs more than a single LLM call runs the *same* self-correcting
graph — the nodes and edges below never change from one source to the next:

    analyze ──> draft ──> critique ──┬──(revise, bounded)──> draft
                                     └──(accept)──> finalize

  analyze   one structured pass over the chunk, producing a source-shaped summary
            (topics, participants, criteria, ...) rather than raw per-item trivia.
  draft     turns that summary into MemoryCardDraft objects.
  critique  an adversarial read: is each card grounded in the source, or inferred?
  finalize  applies the critique, then enforces grounding *deterministically* — an
            `evidence_quote` not in the source is dropped whatever the critic said —
            plus any source-specific keep rule a subclass adds.

A concrete agent changes only *data*, never the graph:

    source_type      the prompt directory under extractors/prompts/
    analysis_schema  the pydantic model the analyze node returns
    prompt_defaults  defaults for optional template variables (StrictUndefined)
    _keep()          an optional extra finalize filter (e.g. attribution)

The graph owns judgement only. Identity, provenance, taxonomy filtering and the
deterministic card id all stay in BaseExtractor._stamp, so an agent can never
spoof them. LLM access goes through LLMClient so every node lands in the same
Langfuse trace as the rest of the pipeline.
"""
from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from learner_memory.core.logging import get_logger
from learner_memory.core.taxonomy import Taxonomy
from learner_memory.extractors.agents._common import (
    Critique,
    last,
    public_context,
    quote_is_grounded,
    sub_trace,
)
from learner_memory.extractors.prompt_loader import PromptLibrary
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.memory_card import ExtractionResult, MemoryCardDraft

log = get_logger(__name__)

MAX_REVISIONS = 1


class GraphState(TypedDict, total=False):
    # inputs (constant for the run)
    chunk: str
    occurred_at: str
    context: dict[str, Any]
    # working values
    analysis: Annotated[BaseModel | None, last]
    drafts: Annotated[list[MemoryCardDraft], last]
    critique: Annotated[Critique | None, last]
    guidance: Annotated[str, last]
    revisions: Annotated[int, last]


class GraphExtractionAgent:
    """Base for every graph-driven extractor. Compiled once per instance; `run` is
    safe to call concurrently. Subclasses set the class attributes and, if needed,
    override `_keep`."""

    source_type: ClassVar[str]
    analysis_schema: ClassVar[type[BaseModel]]
    prompt_defaults: ClassVar[dict[str, Any]] = {"analysis": "", "guidance": ""}

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
        state: GraphState = {
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
        g = StateGraph(GraphState)
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

    async def _analyze(self, state: GraphState) -> dict[str, Any]:
        rendered = self._prompts.render(
            source_type=self.source_type,
            version="v1.analyze",
            context={
                **self._public(state["context"]),
                "chunk": state["chunk"],
                "occurred_at": state["occurred_at"],
            },
        )
        analysis = await self._llm.structured(
            [{"role": "system", "content": rendered.system},
             {"role": "user", "content": rendered.user}],
            schema=self.analysis_schema,
            trace=sub_trace(state["context"]["_trace"], "analyze"),
        )
        log.info("extract.analyzed", source=self.source_type)
        return {"analysis": analysis}

    async def _draft(self, state: GraphState) -> dict[str, Any]:
        analysis = state.get("analysis")
        # A critique already in state means the router sent us back: this pass is a
        # revision, and the critic's guidance is the brief for it.
        previous = state.get("critique")
        revisions = state.get("revisions", 0) + (1 if previous else 0)
        guidance = previous.guidance if previous else ""

        rendered = self._prompts.render(
            source_type=self.source_type,
            version="v1",
            context={
                **self._public(state["context"]),
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
        log.info("extract.drafted", source=self.source_type, cards=len(result.cards),
                 revision=revisions)
        return {"drafts": result.cards, "revisions": revisions, "guidance": guidance}

    async def _critique(self, state: GraphState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        if not drafts:
            # Nothing to argue about; an empty extraction is a valid answer.
            return {"critique": Critique()}

        rendered = self._prompts.render(
            source_type=self.source_type,
            version="v1.critique",
            context={
                **self._public(state["context"]),
                "chunk": state["chunk"],
                "occurred_at": state["occurred_at"],
                "cards": self._render_cards(drafts),
            },
        )
        critique = await self._llm.structured(
            [{"role": "system", "content": rendered.system},
             {"role": "user", "content": rendered.user}],
            schema=Critique,
            trace=sub_trace(state["context"]["_trace"], "critique"),
        )
        rejected = [v.index for v in critique.verdicts if not v.keep]
        log.info("extract.critiqued", source=self.source_type, rejected=len(rejected))
        return {"critique": critique}

    def _route(self, state: GraphState) -> str:
        critique = state.get("critique")
        if critique is None or not critique.verdicts:
            return "accept"
        rejected = [v for v in critique.verdicts if not v.keep]
        if rejected and state.get("revisions", 0) < self._max_revisions:
            return "revise"
        return "accept"

    async def _finalize(self, state: GraphState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        critique = state.get("critique")

        if critique and critique.verdicts:
            dropped = {v.index for v in critique.verdicts if not v.keep}
            drafts = [c for i, c in enumerate(drafts) if i not in dropped]

        kept = [
            c for c in drafts
            if quote_is_grounded(c, state["chunk"]) and self._keep(c, state)
        ]
        if len(kept) != len(drafts):
            log.info("extract.dropped", source=self.source_type, count=len(drafts) - len(kept))
        return {"drafts": kept}

    # ---------------------------------------------------------------- hooks

    def _keep(self, card: MemoryCardDraft, state: GraphState) -> bool:
        """An extra finalize filter beyond grounding. Default keeps everything;
        override for source-specific rules (e.g. attribution to a known subject)."""
        return True

    def _render_cards(self, drafts: list[MemoryCardDraft]) -> str:
        """The drafted cards, as the critic reads them. The subject label is shown
        only when the source sets one, so single-subject sources are unaffected."""
        lines = []
        for i, c in enumerate(drafts):
            subject = f" ({c.subject_label})" if c.subject_label else ""
            contributions = [f"{x.target}:{x.key}@L{x.level_signal}" for x in c.contributions]
            lines.append(
                f"[{i}]{subject} {c.title}\n"
                f"    content: {c.content}\n"
                f"    quote: {c.evidence_quote or '(none)'}\n"
                f"    contributions: {contributions}"
            )
        return "\n".join(lines)

    def _public(self, context: dict[str, Any]) -> dict[str, Any]:
        return public_context(context, self.prompt_defaults)
