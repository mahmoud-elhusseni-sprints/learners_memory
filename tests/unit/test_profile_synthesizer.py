import uuid

import pytest
from pydantic import ValidationError

from learner_memory.profile.evidence import EvidenceInfo, canonical_dimension
from learner_memory.profile.synthesizer import (
    GeneralSkillPatch,
    ProfilePatch,
    ProfileSynthesizer,
)
from learner_memory.profile.updater import validate_patch, with_derived


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def structured(self, messages, *, schema, trace, **_):
        self.calls.append({"messages": messages, "schema": schema, "trace": trace})
        return self.result


async def test_synthesizer_uses_one_structured_call_with_current_profile_and_cards():
    card_id = uuid.uuid4()
    result = ProfilePatch(general_skills=[
        GeneralSkillPatch(
            slug="critical_thinking",
            level=4,
            confidence=0.75,
            evidence_card_ids=[card_id],
            rationale="Repeatedly evaluated conflicting evidence.",
        )
    ])
    llm = FakeLLM(result)
    agent = ProfileSynthesizer(llm=llm)

    actual = await agent.synthesize(
        learner_id=uuid.uuid4(),
        dimensions=["general_skill:critical_thinking"],
        current_profile={"skills": {"general": []}},
        cards=[{"id": card_id, "content": "Compared two conflicting sources."}],
        taxonomy={"critical_thinking": {"level_descriptors": {"4": "Adapts"}}},
    )

    assert actual == result
    assert len(llm.calls) == 1
    assert llm.calls[0]["schema"] is ProfilePatch
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "general_skill:critical_thinking" in prompt
    assert str(card_id) in prompt


def test_patch_rejects_a_card_that_does_not_feed_the_target_dimension():
    card_id = uuid.uuid4()
    patch = ProfilePatch(general_skills=[
        GeneralSkillPatch(
            slug="critical_thinking",
            level=3,
            confidence=0.6,
            evidence_card_ids=[card_id],
            rationale="Evidence",
        )
    ])
    evidence = {
        card_id: EvidenceInfo(
            frozenset({"general_skill:problem_solving"}),
            "task_review",
        )
    }

    with pytest.raises(ValueError, match="does not contribute"):
        validate_patch(patch, {"general_skill:critical_thinking"}, evidence)


def test_patch_rejects_updates_to_dimensions_that_are_not_stale():
    card_id = uuid.uuid4()
    patch = ProfilePatch(general_skills=[
        GeneralSkillPatch(
            slug="critical_thinking",
            level=3,
            confidence=0.6,
            evidence_card_ids=[card_id],
            rationale="Evidence",
        )
    ])
    evidence = {
        card_id: EvidenceInfo(
            frozenset({"general_skill:critical_thinking"}),
            "task_review",
        )
    }

    with pytest.raises(ValueError, match="unstale dimension"):
        validate_patch(patch, {"general_skill:problem_solving"}, evidence)


def test_technical_skill_dimensions_are_canonicalized():
    assert canonical_dimension("technical_skill:Python 3") == "technical_skill:python_3"


def test_evidence_is_required_for_every_change():
    with pytest.raises(ValidationError):
        GeneralSkillPatch(
            slug="critical_thinking",
            level=3,
            confidence=0.6,
            evidence_card_ids=[],
            rationale="Evidence",
        )


def test_rephrased_rationale_does_not_change_existing_derived_metadata():
    card_id = uuid.uuid4()
    original = GeneralSkillPatch(
        slug="critical_thinking",
        level=3,
        confidence=0.6,
        evidence_card_ids=[card_id],
        rationale="Original wording",
    )
    rephrased = original.model_copy(update={"rationale": "Different wording"})

    first = with_derived({}, "critical_thinking", original)
    second = with_derived(first, "critical_thinking", rephrased)

    assert second == first
