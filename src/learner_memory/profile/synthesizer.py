"""Evidence-backed profile synthesis through the shared LLM adapter."""
from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from learner_memory.extractors.prompt_loader import PromptLibrary
from learner_memory.llm.client import LLMClient, TraceContext, get_llm_client

SYNTHESIZER_VERSION = "profile@1.0"
PROMPT_VERSION = "v1"
PROMPT_ROOT = Path(__file__).parent / "prompts"


class EvidencePatch(BaseModel):
    """Base for every proposed change; empty or invented evidence is rejected later."""

    evidence_card_ids: list[uuid.UUID] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class GeneralSkillPatch(EvidencePatch):
    slug: str
    level: int | None = Field(default=None, ge=1, le=6)
    confidence: float = Field(ge=0.0, le=1.0)


class TechnicalSkillPatch(EvidencePatch):
    label: str
    assessed_level: int | None = Field(default=None, ge=1, le=6)
    confidence: float = Field(ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)


class PersonalFieldPatch(EvidencePatch):
    field: str
    value: Any


class LearningPreferencePatch(EvidencePatch):
    key: str
    value: Any


class EntityPatch(EvidencePatch):
    """Patch for a relational career/journey entity.

    ``key`` is the contribution key and ``entity_id`` identifies an existing row.
    A missing entity id requests creation; the deterministic applier still validates
    all values and required foreign keys.
    """

    key: str
    entity_id: uuid.UUID | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class ProfilePatch(BaseModel):
    """Only changed fields are returned; empty lists mean the profile stays as-is."""

    general_skills: list[GeneralSkillPatch] = Field(default_factory=list)
    technical_skills: list[TechnicalSkillPatch] = Field(default_factory=list)
    personal_fields: list[PersonalFieldPatch] = Field(default_factory=list)
    learning_preferences: list[LearningPreferencePatch] = Field(default_factory=list)
    career_goals: list[EntityPatch] = Field(default_factory=list)
    learning_journeys: list[EntityPatch] = Field(default_factory=list)
    journey_steps: list[EntityPatch] = Field(default_factory=list)


class ProfileSynthesizer:
    """One conservative structured-output pass over the targeted profile dimensions."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
    ) -> None:
        self._llm = llm or get_llm_client()
        self._prompts = prompts or PromptLibrary(PROMPT_ROOT)

    async def synthesize(
        self,
        *,
        learner_id: uuid.UUID,
        dimensions: list[str],
        current_profile: dict[str, Any],
        cards: list[dict[str, Any]],
        taxonomy: dict[str, Any],
    ) -> ProfilePatch:
        rendered = self._prompts.render(
            source_type="synthesizer",
            version=PROMPT_VERSION,
            context={
                "dimensions_json": _json(dimensions),
                "current_profile_json": _json(current_profile),
                "cards_json": _json(cards),
                "taxonomy_json": _json(taxonomy),
            },
        )
        return await self._llm.structured(
            [
                {"role": "system", "content": rendered.system},
                {"role": "user", "content": rendered.user},
            ],
            schema=ProfilePatch,
            trace=TraceContext(
                name="profile.synthesize",
                user_id=str(learner_id),
                tags=["profile", "synthesis", SYNTHESIZER_VERSION],
                metadata={
                    "synthesizer_version": SYNTHESIZER_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "dimensions": dimensions,
                },
            ),
        )


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2, sort_keys=True)


@lru_cache
def get_profile_synthesizer() -> ProfileSynthesizer:
    return ProfileSynthesizer()
