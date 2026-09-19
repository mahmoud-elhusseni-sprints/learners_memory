"""The unified memory card. Every source emits this envelope; only `payload` varies."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"
CARD_NAMESPACE = uuid.UUID("6f0f8b1e-0e3a-4a53-9a0b-6a3a0f7b21c4")


class SourceType(StrEnum):
    MEETING_TRANSCRIPT = "meeting_transcript"
    TASK_REVIEW = "task_review"
    CHAT = "chat"
    ASSESSMENT = "assessment"
    CODERBYTE_ASSESSMENT = "coderbyte_assessment"
    CV = "cv"
    SELF_REPORT = "self_report"
    MENTOR_FEEDBACK = "mentor_feedback"
    GENERIC_DOCUMENT = "generic_document"


class CardType(StrEnum):
    OBSERVATION = "observation"
    SKILL_EVIDENCE = "skill_evidence"
    PREFERENCE = "preference"
    GOAL = "goal"
    FACT = "fact"
    MILESTONE = "milestone"
    RISK = "risk"


class CardStatus(StrEnum):
    EXTRACTED = "extracted"
    INDEXED = "indexed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"


ContributionTarget = Literal[
    "general_skill", 
    "technical_skill", 
    "personal_data",
    "learning_preference", 
    "career_goal", 
    "learning_journey", 
    "journey_step",
]


class Contribution(BaseModel):
    """Where this card is *directed* — which profile field it should move."""

    target: ContributionTarget
    key: str                                   # taxonomy slug for general_skill
    level_signal: int | None = Field(None, ge=1, le=6)
    weight: float = Field(0.5, ge=0.0, le=1.0)
    direction: Literal["supports", "contradicts"] = "supports"

    @property
    def index_key(self) -> str:
        """Flattened form stored in Qdrant for single-filter retrieval."""
        return f"{self.target}:{self.key}"


class SourceRef(BaseModel):
    locator: str | None = None                 # e.g. "turn:42-58", "section:experience"
    span: tuple[int, int] | None = None
    timestamp_s: float | None = None


class Validity(BaseModel):
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    superseded_by: uuid.UUID | None = None


class MemoryCardDraft(BaseModel):
    """What an extraction agent returns. No identity/provenance yet — the base
    extractor stamps those, so prompts stay small and agents cannot spoof them."""

    card_type: CardType
    title: str = Field(max_length=120)
    content: str
    evidence_quote: str | None = None
    sentiment: Literal["positive", "neutral", "negative"] | None = None
    contributions: list[Contribution] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    salience: float = Field(0.5, ge=0.0, le=1.0)
    source_ref: SourceRef | None = None
    observed_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    pii_level: Literal["none", "low", "high"] = "none"

    @field_validator("tags")
    @classmethod
    def _dedupe_tags(cls, v: list[str]) -> list[str]:
        return sorted({t.strip().lower() for t in v if t.strip()})


class ExtractionResult(BaseModel):
    """Structured-output envelope requested from the LLM."""

    cards: list[MemoryCardDraft] = Field(default_factory=list)


class MemoryCard(MemoryCardDraft):
    """A persisted card: draft + identity, tenancy and provenance."""

    id: uuid.UUID
    schema_version: str = SCHEMA_VERSION
    organization_id: uuid.UUID
    learner_id: uuid.UUID
    program_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None

    source_type: SourceType
    source_document_id: uuid.UUID
    ingested_at: datetime
    extractor_version: str
    prompt_version: str
    model: str
    status: CardStatus = CardStatus.EXTRACTED
    validity: Validity = Field(default_factory=Validity)

    @staticmethod
    def deterministic_id(document_id: uuid.UUID, local_key: str) -> uuid.UUID:
        """Same document + same chunk anchor ⇒ same card id, so re-extraction
        upserts instead of duplicating."""
        return uuid.uuid5(CARD_NAMESPACE, f"{document_id}:{local_key}")

    def contribution_keys(self) -> list[str]:
        return [c.index_key for c in self.contributions]
