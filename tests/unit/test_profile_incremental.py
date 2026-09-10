import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from learner_memory.db.models.learner import SkillAssessment
from learner_memory.db.models.raw import CardContribution
from learner_memory.db.repositories.profile import (
    CardContributionVersion,
    ProfileRepository,
)
from learner_memory.profile import evidence as evidence_module
from learner_memory.profile import service as service_module
from learner_memory.profile.evidence import (
    EvidenceInfo,
    FreshEvidenceBatch,
    load_fresh_evidence,
)
from learner_memory.profile.synthesizer import GeneralSkillPatch, ProfilePatch
from learner_memory.profile.updater import apply_patch, with_derived


def _card(confidence: float, *, observed_at: datetime | None = None):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_type="assessment",
        card_type="skill_evidence",
        title="Evidence",
        content="Demonstrated the skill.",
        evidence_quote="Demonstrated the skill.",
        observed_at=observed_at or now,
        updated_at=now,
        confidence=confidence,
        salience=1.0,
        payload={},
    )


def _contribution(key: str):
    return SimpleNamespace(
        target="general_skill",
        key=key,
        level_signal=3,
        weight=1.0,
        direction="supports",
    )


async def test_fresh_evidence_caps_and_tracks_only_selected_contributions(monkeypatch):
    cards = [_card(0.9), _card(0.8), _card(0.7)]

    class Repository:
        async def fresh_card_contributions(self, **_):
            return [(card, _contribution("critical_thinking")) for card in cards]

    monkeypatch.setattr(evidence_module, "MAX_FRESH_CONTRIBUTIONS_PER_RUN", 2)
    batch = await load_fresh_evidence(
        Repository(),
        learner_id=uuid.uuid4(),
        dimensions=["general_skill:critical_thinking"],
    )

    assert [item["id"] for item in batch.cards] == [str(cards[0].id), str(cards[1].id)]
    assert [item.card_id for item in batch.contributions] == [cards[0].id, cards[1].id]
    assert cards[2].id not in batch.evidence


async def test_consumption_marker_records_the_seen_card_version_without_regressing():
    card_id = uuid.uuid4()
    seen_at = datetime.now(UTC)
    row = CardContribution(
        card_id=card_id,
        target="general_skill",
        key="critical_thinking",
        level_signal=3,
        weight=1.0,
        direction="supports",
    )

    class Result:
        def scalars(self):
            return [row]

    class Session:
        async def execute(self, _):
            return Result()

    repository = ProfileRepository(Session(), uuid.uuid4())
    await repository.mark_card_contributions_consumed(
        [
            CardContributionVersion(
                card_id=card_id,
                target="general_skill",
                key="critical_thinking",
                card_updated_at=seen_at,
            )
        ]
    )
    assert row.profile_consumed_card_updated_at == seen_at

    await repository.mark_card_contributions_consumed(
        [
            CardContributionVersion(
                card_id=card_id,
                target="general_skill",
                key="critical_thinking",
                card_updated_at=seen_at - timedelta(days=1),
            )
        ]
    )
    assert row.profile_consumed_card_updated_at == seen_at


async def test_general_skill_keeps_previous_evidence_when_fresh_evidence_changes_it():
    old_card_id = uuid.uuid4()
    fresh_card_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    current = SkillAssessment(
        organization_id=uuid.uuid4(),
        learner_id=uuid.uuid4(),
        skill_id=skill_id,
        level=2,
        confidence=0.5,
        evidence_card_ids=[old_card_id],
        synthesizer_version="profile@1.0",
        taxonomy_version="1.0",
        computed_at=datetime.now(UTC),
        superseded=False,
    )

    class Repository:
        organization_id = current.organization_id

        def __init__(self):
            self.stored = []

        async def skill_catalog(self, _):
            return SimpleNamespace(id=skill_id, taxonomy_version="1.0")

        async def current_assessment(self, *_):
            return current

        def store(self, value):
            self.stored.append(value)

    repository = Repository()
    patch = ProfilePatch(
        general_skills=[
            GeneralSkillPatch(
                slug="critical_thinking",
                level=3,
                confidence=0.7,
                evidence_card_ids=[fresh_card_id],
                rationale="Fresh assessment demonstrates the next level.",
            )
        ]
    )
    changed = await apply_patch(
        repository,
        learner_id=current.learner_id,
        patch=patch,
        evidence={
            fresh_card_id: EvidenceInfo(
                frozenset({"general_skill:critical_thinking"}), "assessment"
            )
        },
    )

    assert changed is True
    assert current.superseded is True
    assert repository.stored[0].evidence_card_ids == sorted([old_card_id, fresh_card_id], key=str)


def test_derived_metadata_accumulates_only_cited_fresh_evidence():
    old_card_id = uuid.uuid4()
    fresh_card_id = uuid.uuid4()
    existing = {
        "_derived": {
            "target_role": {
                "evidence_card_ids": [str(old_card_id)],
                "synthesizer_version": "profile@1.0",
                "rationale": "Old evidence",
            }
        }
    }
    patch = GeneralSkillPatch(
        slug="critical_thinking",
        level=3,
        confidence=0.7,
        evidence_card_ids=[fresh_card_id],
        rationale="Fresh evidence",
    )

    updated = with_derived(existing, "target_role", patch)

    assert updated["_derived"]["target_role"]["evidence_card_ids"] == sorted(
        [str(old_card_id), str(fresh_card_id)]
    )


@pytest.mark.parametrize("has_more", [False, True])
async def test_empty_patch_consumes_fresh_cards_without_erasing_profile(monkeypatch, has_more):
    learner_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    card_id = uuid.uuid4()
    dimension = "general_skill:critical_thinking"
    card_updated_at = datetime.now(UTC)
    profile = SimpleNamespace(
        stale_dimensions=[dimension],
        profile_version=4,
        snapshot={"skills": {"general": [{"slug": "critical_thinking", "level": 3}]}},
        computed_at=None,
    )
    consumed = []

    class Repository:
        async def profile(self, *_args, **_kwargs):
            return profile

        async def mark_card_contributions_consumed(self, versions):
            consumed.extend(versions)

        async def dimensions_with_fresh_contributions(self, **_):
            return {dimension} if has_more else set()

        async def flush(self):
            return None

    repository = Repository()

    @asynccontextmanager
    async def fake_unit_of_work():
        yield SimpleNamespace()

    async def fake_state(*_args):
        return ({**profile.snapshot, "read_model": {}}, {})

    async def fake_evidence(*_args, **_kwargs):
        return FreshEvidenceBatch(
            cards=[{"id": str(card_id), "content": "No new profile information."}],
            evidence={card_id: EvidenceInfo(frozenset({dimension}), "assessment")},
            contributions=[
                CardContributionVersion(
                    card_id=card_id,
                    target="general_skill",
                    key="critical_thinking",
                    card_updated_at=card_updated_at,
                )
            ],
            since=card_updated_at - timedelta(days=30),
        )

    async def no_journey_work(*_args):
        return None

    class Synthesizer:
        async def synthesize(self, **_):
            return ProfilePatch()

    monkeypatch.setattr(service_module, "unit_of_work", fake_unit_of_work)
    monkeypatch.setattr(service_module, "ProfileRepository", lambda *_: repository)
    monkeypatch.setattr(service_module, "load_profile_state", fake_state)
    monkeypatch.setattr(service_module, "load_fresh_evidence", fake_evidence)
    monkeypatch.setattr(service_module, "recalculate_journeys", no_journey_work)

    result = await service_module.recompute_profile(
        organization_id,
        learner_id,
        [dimension],
        synthesizer=Synthesizer(),
    )

    assert result["changed"] is False
    assert result["profile_version"] == 4
    assert consumed[0].card_id == card_id
    assert profile.snapshot == {"skills": {"general": [{"slug": "critical_thinking", "level": 3}]}}
    assert result["pending_dimensions"] == ([dimension] if has_more else [])
    assert profile.stale_dimensions == ([dimension] if has_more else [])
