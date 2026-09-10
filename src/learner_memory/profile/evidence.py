"""Retrieve and rank memory-card evidence for stale profile dimensions."""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from learner_memory.core.config import get_settings
from learner_memory.db.models.raw import MemoryCardRecord
from learner_memory.db.repositories.profile import (
    CardContributionVersion,
    ProfileRepository,
)
from learner_memory.profile.serialization import jsonable

MAX_FRESH_CONTRIBUTIONS_PER_RUN = 50
SUPPORTED_TARGETS = {
    "general_skill",
    "technical_skill",
    "personal_data",
    "learning_preference",
    "career_goal",
    "learning_journey",
    "journey_step",
}


@dataclass(frozen=True)
class EvidenceInfo:
    dimensions: frozenset[str]
    source_type: str


@dataclass(frozen=True)
class FreshEvidenceBatch:
    cards: list[dict[str, Any]]
    evidence: dict[uuid.UUID, EvidenceInfo]
    contributions: list[CardContributionVersion]
    since: datetime


async def load_fresh_evidence(
    repository: ProfileRepository,
    *,
    learner_id: uuid.UUID,
    dimensions: list[str],
) -> FreshEvidenceBatch:
    parsed = [parse_dimension(dimension) for dimension in dimensions]
    settings = get_settings()
    now = datetime.now(UTC)
    since = now - timedelta(days=settings.evidence_window_months * 30)
    rows = await repository.fresh_card_contributions(
        learner_id=learner_id,
        dimensions=parsed,
        since=since,
        now=now,
    )

    ranked: list[tuple[float, dict[str, Any], MemoryCardRecord, str, str]] = []
    for card, contribution in rows:
        age_days = max(0.0, (now - card.observed_at).total_seconds() / 86400)
        recency = math.pow(0.5, age_days / settings.decay_half_life_days) # recency factor based on half-life decay = 0.5^(age_days / half_life_days)
        effective_weight = card.confidence * card.salience * contribution.weight * recency # effective weight = confidence * salience * contribution weight * recency factor
        item = {
            "id": card.id,
            "source_type": card.source_type,
            "card_type": card.card_type,
            "title": card.title,
            "content": card.content,
            "evidence_quote": card.evidence_quote,
            "observed_at": card.observed_at,
            "confidence": card.confidence,
            "salience": card.salience,
            "payload": card.payload,
            "contribution": {
                "target": contribution.target,
                "key": contribution.key,
                "level_signal": contribution.level_signal,
                "weight": contribution.weight,
                "direction": contribution.direction,
                "effective_weight": round(effective_weight, 6),
            },
        }
        ranked.append((
            effective_weight,
            item,
            card,
            contribution.target,
            contribution.key,
        ))

    cards: list[dict[str, Any]] = []
    consumed: list[CardContributionVersion] = []
    evidence_dimensions: dict[uuid.UUID, set[str]] = {}
    evidence_sources: dict[uuid.UUID, str] = {}
    selected = sorted(ranked, key=lambda item: item[0], reverse=True)[
        :MAX_FRESH_CONTRIBUTIONS_PER_RUN
    ]
    for _, item, record, target, key in selected:
        dimension = f"{target}:{key}"
        cards.append(jsonable(item))
        consumed.append(CardContributionVersion(
            card_id=record.id,
            target=target,
            key=key,
            card_updated_at=record.updated_at,
        ))
        evidence_dimensions.setdefault(record.id, set()).add(dimension)
        evidence_sources[record.id] = record.source_type

    evidence = {
        card_id: EvidenceInfo(frozenset(card_dimensions), evidence_sources[card_id])
        for card_id, card_dimensions in evidence_dimensions.items()
    }
    return FreshEvidenceBatch(
        cards=cards,
        evidence=evidence,
        contributions=consumed,
        since=since,
    )


def parse_dimension(dimension: str) -> tuple[str, str]:
    try:
        target, key = dimension.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"invalid profile dimension: {dimension}") from exc
    if target not in SUPPORTED_TARGETS or not key:
        raise ValueError(f"invalid profile dimension: {dimension}")
    return target, key


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def canonical_dimension(dimension: str) -> str:
    target, key = parse_dimension(dimension)
    if target == "technical_skill":
        key = normalize_key(key)
    return f"{target}:{key}"
