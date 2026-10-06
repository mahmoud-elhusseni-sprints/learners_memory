"""Agent for asynchronous learner interactions with support roles."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from learner_memory.extractors.agents.graph import GraphExtractionAgent


class InteractionSignal(BaseModel):
    dimension: Literal[
        "engagement", "responsiveness", "communication", "follow_through", "support_need", "other"
    ]
    observation: str = Field(description="Grounded behavior, timing pattern, or support need")
    message_indexes: list[int] = Field(default_factory=list)


class ChatAnalysis(BaseModel):
    signals: list[InteractionSignal] = Field(default_factory=list)
    unresolved_requests: list[str] = Field(default_factory=list)
    overall: str = ""


class ChatAgent(GraphExtractionAgent):
    source_type = "chat"
    analysis_schema = ChatAnalysis
    prompt_defaults: ClassVar[dict[str, Any]] = {
        "conversation_id": None,
        "channel": None,
        "learner_sender_id": None,
        "analysis": "",
        "guidance": "",
    }
