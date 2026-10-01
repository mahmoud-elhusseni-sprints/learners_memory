"""Primitives shared by the LangGraph extraction agents.

Both the assessment and the transcript agent run the same shape of graph —
analyze -> draft -> critique -> (revise | finalize) — and share the same
critique contract and the same deterministic grounding backstop. Those pieces
live here so a second agent does not copy them, while each agent keeps its own
source-specific analysis and prompts.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from learner_memory.llm.client import TraceContext
from learner_memory.schemas.memory_card import MemoryCardDraft


def last(_: Any, new: Any) -> Any:
    """LangGraph reducer: a node's value replaces the previous one."""
    return new


class CardVerdict(BaseModel):
    index: int = Field(description="Position of the card in the drafted list, zero-based")
    keep: bool
    reason: str = ""


class Critique(BaseModel):
    verdicts: list[CardVerdict] = Field(default_factory=list)
    guidance: str = Field("", description="What to fix if the drafts should be revised")


def public_context(context: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Prompt variables only — strips the trace object smuggled through graph state,
    and fills defaults so StrictUndefined stays useful for catching real typos."""
    return {**defaults, **{k: v for k, v in context.items() if not k.startswith("_")}}


def sub_trace(parent: TraceContext, node: str) -> TraceContext:
    """Each graph node is its own generation under the extractor's trace."""
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


def quote_is_grounded(card: MemoryCardDraft, source: str) -> bool:
    """A quote that is not in the source is a fabrication, whatever the critic says.

    Whitespace is normalized because models reflow code and prose freely; anything
    beyond that (paraphrase, ellipsis, invented text) fails.
    """
    if not card.evidence_quote:
        return True
    return _normalize(card.evidence_quote) in _normalize(source)
