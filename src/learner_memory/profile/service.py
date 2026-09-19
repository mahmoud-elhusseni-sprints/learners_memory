"""Application service coordinating one L3 profile recomputation."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from learner_memory.core.logging import get_logger
from learner_memory.db.repositories.profile import ProfileRepository
from learner_memory.db.session import unit_of_work
from learner_memory.profile.evidence import load_fresh_evidence, parse_dimension
from learner_memory.profile.state import load_profile_state
from learner_memory.profile.synthesizer import (
    ProfilePatch,
    ProfileSynthesizer,
    get_profile_synthesizer,
)
from learner_memory.profile.updater import (
    apply_patch,
    recalculate_journeys,
    validate_patch,
)

log = get_logger(__name__)


async def recompute_profile(
    organization_id: uuid.UUID,
    learner_id: uuid.UUID,
    dimensions: list[str] | None,
    *,
    synthesizer: ProfileSynthesizer | None = None,
) -> dict[str, Any]:
    """Load, synthesize, and atomically apply currently stale dimensions."""
    async with unit_of_work() as session:
        repository = ProfileRepository(session, organization_id)
        profile = await repository.profile(learner_id)
        if profile is None:
            log.warning(
                "profile.unknown_learner",
                organization_id=str(organization_id),
                learner_id=str(learner_id),
            )
            return {"skipped": True, "reason": "unknown_learner"}

        stale = set(profile.stale_dimensions or [])
        requested = stale if dimensions is None else stale.intersection(dimensions)
        requested_dimensions = sorted(requested)
        if not requested_dimensions:
            return {"skipped": True, "reason": "not_stale", "learner_id": str(learner_id)}

        base_version = profile.profile_version
        current_profile, taxonomy = await load_profile_state(repository, learner_id, profile)
        targeted_skills = {
            key
            for target, key in map(parse_dimension, requested_dimensions)
            if target == "general_skill"
        }
        taxonomy = {slug: value for slug, value in taxonomy.items() if slug in targeted_skills}
        batch = await load_fresh_evidence(
            repository,
            learner_id=learner_id,
            dimensions=requested_dimensions,
        )

    proposed = ProfilePatch()
    if batch.cards:
        proposed = await (synthesizer or get_profile_synthesizer()).synthesize(
            learner_id=learner_id,
            dimensions=requested_dimensions,
            current_profile=current_profile,
            cards=batch.cards,
            taxonomy=taxonomy,
        )
    validate_patch(proposed, set(requested_dimensions), batch.evidence)

    async with unit_of_work() as session:
        repository = ProfileRepository(session, organization_id)
        locked = await repository.profile(learner_id, for_update=True)
        if locked is None:
            return {"skipped": True, "reason": "unknown_learner"}
        if locked.profile_version != base_version:
            log.info(
                "profile.concurrent_update",
                learner_id=str(learner_id),
                expected=base_version,
                actual=locked.profile_version,
            )
            return {"retry": True, "reason": "profile_changed"}

        changed = await apply_patch(
            repository,
            learner_id=learner_id,
            patch=proposed,
            evidence=batch.evidence,
        )
        await recalculate_journeys(repository, learner_id)
        await repository.mark_card_contributions_consumed(batch.contributions)
        await repository.flush()

        remaining_dimensions = await repository.dimensions_with_fresh_contributions(
            learner_id=learner_id,
            dimensions=[parse_dimension(value) for value in requested_dimensions],
            since=batch.since,
            now=datetime.now(UTC),
        )

        rebuilt, _ = await load_profile_state(repository, learner_id, locked)
        snapshot = {key: value for key, value in rebuilt.items() if key != "read_model"}
        snapshot_changed = snapshot != (locked.snapshot or {})
        if changed or snapshot_changed:
            locked.snapshot = snapshot
            locked.profile_version += 1
        locked.computed_at = datetime.now(UTC)
        locked.stale_dimensions = sorted(
            (set(locked.stale_dimensions or []) - set(requested_dimensions))
            | remaining_dimensions
        )
        result = {
            "learner_id": str(learner_id),
            "processed_dimensions": requested_dimensions,
            "cards_considered": len(batch.cards),
            "pending_dimensions": sorted(remaining_dimensions),
            "changed": changed or snapshot_changed,
            "profile_version": locked.profile_version,
        }
    log.info("profile.recomputed", **result)
    return result
