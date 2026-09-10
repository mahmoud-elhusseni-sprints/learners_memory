"""Validate and apply an agent-proposed profile patch through the repository."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from learner_memory.db.models.learner import (
    CareerGoal,
    JourneyStep,
    LearnerTechnicalSkill,
    LearningJourney,
    SkillAssessment,
)
from learner_memory.db.repositories.profile import ProfileRepository
from learner_memory.profile.evidence import (
    EvidenceInfo,
    canonical_dimension,
    normalize_key,
)
from learner_memory.profile.serialization import jsonable
from learner_memory.profile.synthesizer import (
    SYNTHESIZER_VERSION,
    EntityPatch,
    EvidencePatch,
    GeneralSkillPatch,
    LearningPreferencePatch,
    PersonalFieldPatch,
    ProfilePatch,
    TechnicalSkillPatch,
)

CONFIDENCE_EPSILON = 0.01
PERSONAL_FIELDS = {
    "email", "phone", "location", "contact", "education", "experience", "languages",
}
CAREER_FIELDS = {"title", "target_role", "target_date", "motivation", "status", "details"}
JOURNEY_FIELDS = {"career_goal_id", "name", "status", "plan"}
STEP_FIELDS = {"journey_id", "ord", "title", "kind", "status", "completed_at", "details"}
SOURCE_PRIORITY = {
    "inferred": 10,
    "meeting_transcript": 20,
    "task_review": 30,
    "assessment": 40,
    "coderbyte_assessment": 40,
    "mentor_feedback": 60,
    "cv": 70,
    "self_report": 80,
    "human": 100,
}


def validate_patch(
    patch: ProfilePatch,
    requested: set[str],
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> None:
    canonical_requested = {canonical_dimension(dimension) for dimension in requested}
    checks: list[tuple[str, EvidencePatch]] = []
    checks.extend((f"general_skill:{item.slug}", item) for item in patch.general_skills)
    checks.extend(
        (f"technical_skill:{normalize_key(item.label)}", item)
        for item in patch.technical_skills
    )
    checks.extend((f"personal_data:{item.field}", item) for item in patch.personal_fields)
    checks.extend(
        (f"learning_preference:{item.key}", item) for item in patch.learning_preferences
    )
    checks.extend((f"career_goal:{item.key}", item) for item in patch.career_goals)
    checks.extend(
        (f"learning_journey:{item.key}", item) for item in patch.learning_journeys
    )
    checks.extend((f"journey_step:{item.key}", item) for item in patch.journey_steps)

    seen: set[str] = set()
    for dimension, item in checks:
        canonical = canonical_dimension(dimension)
        if canonical in seen:
            raise ValueError(f"profile patch repeated dimension: {dimension}")
        seen.add(canonical)
        if canonical not in canonical_requested:
            raise ValueError(f"profile patch targeted an unstale dimension: {dimension}")
        for card_id in item.evidence_card_ids:
            info = evidence.get(card_id)
            if info is None:
                raise ValueError(f"profile patch cited an unavailable card: {card_id}")
            if canonical not in {canonical_dimension(value) for value in info.dimensions}:
                raise ValueError(f"card {card_id} does not contribute to {dimension}")


async def apply_patch(
    repository: ProfileRepository,
    *,
    learner_id: uuid.UUID,
    patch: ProfilePatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> bool:
    changed = False
    for skill_patch in patch.general_skills:
        changed |= await _apply_general_skill(repository, learner_id, skill_patch)
    for technical_patch in patch.technical_skills:
        changed |= await _apply_technical_skill(repository, learner_id, technical_patch)
    for personal_patch in patch.personal_fields:
        changed |= await _apply_personal_field(
            repository, learner_id, personal_patch, evidence
        )
    for preference_patch in patch.learning_preferences:
        changed |= await _apply_preference(repository, learner_id, preference_patch, evidence)
    for goal_patch in patch.career_goals:
        changed |= await _apply_career_goal(repository, learner_id, goal_patch, evidence)
    for journey_patch in patch.learning_journeys:
        changed |= await _apply_journey(repository, learner_id, journey_patch, evidence)
    for step_patch in patch.journey_steps:
        changed |= await _apply_step(repository, learner_id, step_patch, evidence)
    return changed


async def recalculate_journeys(
    repository: ProfileRepository, learner_id: uuid.UUID
) -> None:
    for journey in await repository.journeys(learner_id):
        steps = await repository.journey_steps(journey.id)
        if not steps:
            continue
        done = sum(step.status in {"done", "skipped"} for step in steps)
        journey.progress = done / len(steps)
        status_source = (journey.plan or {}).get("source_of_truth", {}).get("status")
        if done == len(steps) and journey.status == "active" and status_source != "human":
            journey.status = "completed"


async def _apply_general_skill(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: GeneralSkillPatch,
) -> bool:
    catalog = await repository.skill_catalog(item.slug)
    if catalog is None:
        raise ValueError(f"unknown general skill: {item.slug}")
    current = await repository.current_assessment(learner_id, catalog.id)
    evidence_ids = _merged_evidence_ids(
        current.evidence_card_ids if current else [], item.evidence_card_ids
    )
    if (
        current
        and current.level is not None
        and item.level is not None
        and item.level > current.level + 1
    ):
        raise ValueError(
            f"skill level cannot rise more than one step: {item.slug} "
            f"{current.level}->{item.level}"
        )
    if current and (
        current.level == item.level
        and abs(current.confidence - item.confidence) < CONFIDENCE_EPSILON
        and sorted(current.evidence_card_ids, key=str) == evidence_ids
    ):
        return False
    if current:
        current.superseded = True
    repository.store(SkillAssessment(
        organization_id=repository.organization_id,
        learner_id=learner_id,
        skill_id=catalog.id,
        level=item.level,
        confidence=item.confidence,
        evidence_card_ids=evidence_ids,
        rationale=item.rationale,
        synthesizer_version=SYNTHESIZER_VERSION,
        taxonomy_version=catalog.taxonomy_version,
        computed_at=datetime.now(UTC),
    ))
    return True


async def _apply_technical_skill(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: TechnicalSkillPatch,
) -> bool:
    normalized = normalize_key(item.label)
    rows = await repository.technical_skills(learner_id)
    current = next((row for row in rows if normalize_key(row.label) == normalized), None)
    evidence_ids = _merged_evidence_ids(
        current.evidence_card_ids if current else [], item.evidence_card_ids
    )
    details = dict(current.details or {}) if current else {}
    details.update(item.details)
    details = with_derived(details, normalized, item)
    if current is None:
        repository.store(LearnerTechnicalSkill(
            learner_id=learner_id,
            label=normalized,
            assessed_level=item.assessed_level,
            confidence=item.confidence,
            evidence_card_ids=evidence_ids,
            last_evidence_at=datetime.now(UTC),
            details=details,
        ))
        return True
    if (
        current.assessed_level == item.assessed_level
        and abs(current.confidence - item.confidence) < CONFIDENCE_EPSILON
        and sorted(current.evidence_card_ids, key=str) == evidence_ids
        and (current.details or {}) == details
    ):
        return False
    current.assessed_level = item.assessed_level
    current.confidence = item.confidence
    current.evidence_card_ids = evidence_ids
    current.last_evidence_at = datetime.now(UTC)
    current.details = details
    return True


async def _apply_personal_field(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: PersonalFieldPatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> bool:
    if item.field not in PERSONAL_FIELDS:
        raise ValueError(f"unsupported personal field: {item.field}")
    personal = await repository.personal_data(learner_id)
    truth = dict(personal.source_of_truth or {})
    source = _strongest_source(item.evidence_card_ids, evidence)
    current_source = str(truth.get(item.field, "inferred") or "inferred")
    if current_source == "human" or _source_rank(source) < _source_rank(current_source):
        return False
    value = jsonable(item.value)
    if getattr(personal, item.field) == value and current_source == source:
        return False
    setattr(personal, item.field, value)
    truth[item.field] = source
    personal.source_of_truth = truth
    return True


async def _apply_preference(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: LearningPreferencePatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> bool:
    personal = await repository.personal_data(learner_id)
    truth = dict(personal.source_of_truth or {})
    truth_key = f"learning_preferences.{item.key}"
    current_source = str(
        truth.get(truth_key, truth.get("learning_preferences", "inferred")) or "inferred"
    )
    source = _strongest_source(item.evidence_card_ids, evidence)
    if current_source == "human" or _source_rank(source) < _source_rank(current_source):
        return False
    preferences = dict(personal.learning_preferences or {})
    value = jsonable(item.value)
    if _get_nested(preferences, item.key) == value and truth.get(truth_key) == source:
        return False
    _set_nested(preferences, item.key, value)
    truth[truth_key] = source
    personal.learning_preferences = preferences
    personal.source_of_truth = truth
    return True


async def _apply_career_goal(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: EntityPatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> bool:
    values = _only_fields(item.values, CAREER_FIELDS, "career goal")
    creating = item.entity_id is None
    _validate_field_directed_values(
        item.key, values, CAREER_FIELDS, "details", {"title"} if creating else set()
    )
    goal = await repository.career_goal(item.entity_id, learner_id) if item.entity_id else None
    if item.entity_id and goal is None:
        raise ValueError(f"career goal does not belong to learner: {item.entity_id}")
    if goal is None:
        if not values.get("title"):
            raise ValueError("a new career goal requires title")
        goal = CareerGoal(learner_id=learner_id, title=str(values.pop("title")))
        repository.store(goal)
    values = _coerce_values(values, date_fields={"target_date"})
    return _apply_entity_values(goal, values, item, evidence, "details", creating)


async def _apply_journey(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: EntityPatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> bool:
    values = _only_fields(item.values, JOURNEY_FIELDS, "learning journey")
    creating = item.entity_id is None
    _validate_field_directed_values(
        item.key,
        values,
        JOURNEY_FIELDS,
        "plan",
        {"name", "career_goal_id"} if creating else set(),
    )
    journey = await repository.journey(item.entity_id, learner_id) if item.entity_id else None
    if item.entity_id and journey is None:
        raise ValueError(f"learning journey does not belong to learner: {item.entity_id}")
    if "career_goal_id" in values and values["career_goal_id"] is not None:
        goal_id = uuid.UUID(str(values["career_goal_id"]))
        if await repository.career_goal(goal_id, learner_id) is None:
            raise ValueError("journey career goal does not belong to learner")
        values["career_goal_id"] = goal_id
    if journey is None:
        if not values.get("name"):
            raise ValueError("a new learning journey requires name")
        journey = LearningJourney(learner_id=learner_id, name=str(values.pop("name")))
        repository.store(journey)
    return _apply_entity_values(journey, values, item, evidence, "plan", creating)


async def _apply_step(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    item: EntityPatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
) -> bool:
    values = _only_fields(item.values, STEP_FIELDS, "journey step")
    creating = item.entity_id is None
    step = await repository.journey_step(item.entity_id, learner_id) if item.entity_id else None
    if item.entity_id and step is None:
        raise ValueError(f"unknown journey step: {item.entity_id}")
    if step is not None:
        values.pop("journey_id", None)
    else:
        if not values.get("journey_id") or not values.get("title"):
            raise ValueError("a new journey step requires journey_id and title")
        journey_id = uuid.UUID(str(values.pop("journey_id")))
        if await repository.journey(journey_id, learner_id) is None:
            raise ValueError("journey step's journey does not belong to learner")
        step = JourneyStep(
            journey_id=journey_id,
            title=str(values.pop("title")),
            evidence_card_ids=[],
        )
        repository.store(step)
    values = _coerce_values(values, datetime_fields={"completed_at"})
    evidence_ids = _merged_evidence_ids(
        step.evidence_card_ids or [], item.evidence_card_ids
    )
    changed = _apply_entity_values(step, values, item, evidence, "details", creating)
    if sorted(step.evidence_card_ids or [], key=str) != evidence_ids:
        step.evidence_card_ids = evidence_ids
        changed = True
    if values.get("status") == "done" and step.completed_at is None:
        step.completed_at = datetime.now(UTC)
        changed = True
    return changed


def _apply_entity_values(
    entity: Any,
    values: dict[str, Any],
    item: EntityPatch,
    evidence: dict[uuid.UUID, EvidenceInfo],
    metadata_field: str,
    creating: bool,
) -> bool:
    metadata = dict(getattr(entity, metadata_field, {}) or {})
    truth = dict(metadata.get("source_of_truth", {}))
    source = _strongest_source(item.evidence_card_ids, evidence)
    changed = creating
    eligible = creating
    for field, value in values.items():
        if field == metadata_field:
            continue
        current_source = str(truth.get(field, "inferred") or "inferred")
        if current_source == "human" or _source_rank(source) < _source_rank(current_source):
            continue
        eligible = True
        if getattr(entity, field) != value:
            setattr(entity, field, value)
            changed = True
        truth[field] = source

    supplied_metadata = values.get(metadata_field)
    if supplied_metadata is not None:
        if not isinstance(supplied_metadata, dict):
            raise ValueError(f"{metadata_field} must be an object")
        metadata_source = str(truth.get(metadata_field, "inferred") or "inferred")
        if metadata_source != "human" and _source_rank(source) >= _source_rank(metadata_source):
            eligible = True
            truth[metadata_field] = source
            for key, value in supplied_metadata.items():
                if key not in {"_derived", "source_of_truth"}:
                    metadata[key] = jsonable(value)
    if eligible:
        metadata["source_of_truth"] = truth
        new_metadata = with_derived(metadata, item.key, item)
        if new_metadata != (getattr(entity, metadata_field, {}) or {}):
            setattr(entity, metadata_field, new_metadata)
            changed = True
    return changed


def with_derived(data: dict[str, Any], key: str, item: EvidencePatch) -> dict[str, Any]:
    result = dict(data)
    derived = dict(result.get("_derived", {}))
    old = derived.get(key, {})
    evidence_card_ids = sorted({
        *[str(card_id) for card_id in (old.get("evidence_card_ids") or [])],
        *[str(card_id) for card_id in item.evidence_card_ids],
    })
    identity = {
        "evidence_card_ids": evidence_card_ids,
        "synthesizer_version": SYNTHESIZER_VERSION,
    }
    old_identity = {
        "evidence_card_ids": old.get("evidence_card_ids"),
        "synthesizer_version": old.get("synthesizer_version"),
    }
    identity["rationale"] = old.get("rationale") if old_identity == identity else item.rationale
    derived[key] = identity
    result["_derived"] = derived
    return result


def _merged_evidence_ids(
    existing: list[uuid.UUID], fresh: list[uuid.UUID]
) -> list[uuid.UUID]:
    return sorted(set(existing) | set(fresh), key=str)


def _strongest_source(
    card_ids: list[uuid.UUID], evidence: dict[uuid.UUID, EvidenceInfo]
) -> str:
    return max(
        (evidence[card_id].source_type for card_id in card_ids),
        key=_source_rank,
        default="inferred",
    )


def _source_rank(source: str) -> int:
    return SOURCE_PRIORITY.get(source, SOURCE_PRIORITY["inferred"])


def _only_fields(values: dict[str, Any], allowed: set[str], label: str) -> dict[str, Any]:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported {label} fields: {sorted(unknown)}")
    return dict(values)


def _validate_field_directed_values(
    key: str,
    values: dict[str, Any],
    fields: set[str],
    metadata_field: str,
    creation_fields: set[str],
) -> None:
    if key not in fields:
        return
    unrelated = set(values) - {key, metadata_field, *creation_fields}
    if unrelated:
        raise ValueError(
            f"contribution {key} cannot update unrelated fields: {sorted(unrelated)}"
        )


def _coerce_values(
    values: dict[str, Any],
    *,
    date_fields: set[str] | None = None,
    datetime_fields: set[str] | None = None,
) -> dict[str, Any]:
    result = dict(values)
    for field in (date_fields or set()) & result.keys():
        if result[field] is not None and not isinstance(result[field], date):
            result[field] = date.fromisoformat(str(result[field]))
    for field in (datetime_fields or set()) & result.keys():
        if result[field] is not None and not isinstance(result[field], datetime):
            result[field] = datetime.fromisoformat(str(result[field]))
    return result


def _get_nested(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value
