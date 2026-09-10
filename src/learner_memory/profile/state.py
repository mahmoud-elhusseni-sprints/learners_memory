"""Build the canonical L3 state passed to the agent and stored as the read model."""
from __future__ import annotations

import uuid
from typing import Any

from learner_memory.db.models.learner import LearnerProfile
from learner_memory.db.repositories.profile import ProfileRepository
from learner_memory.profile.serialization import jsonable


async def load_profile_state(
    repository: ProfileRepository,
    learner_id: uuid.UUID,
    profile: LearnerProfile,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = await repository.state_rows(learner_id)
    general = []
    taxonomy: dict[str, Any] = {}
    for catalog in rows.catalogs:
        assessment = rows.assessments.get(catalog.slug)
        taxonomy[catalog.slug] = {
            "name": catalog.name,
            "category": catalog.category,
            "level_descriptors": catalog.level_descriptors,
        }
        general.append({
            "slug": catalog.slug,
            "name": catalog.name,
            "category": catalog.category,
            "level": assessment.level if assessment else None,
            "confidence": assessment.confidence if assessment else 0.0,
            "evidence_card_ids": assessment.evidence_card_ids if assessment else [],
            "rationale": assessment.rationale if assessment else None,
            "computed_at": assessment.computed_at if assessment else None,
        })

    state = {
        "learner": {
            "id": rows.learner.id,
            "display_name": rows.learner.display_name,
            "program_id": rows.learner.program_id,
            "cohort_id": rows.learner.cohort_id,
            "metadata": rows.learner.metadata_,
        },
        "personal": {
            "email": rows.personal.email,
            "phone": rows.personal.phone,
            "location": rows.personal.location,
            "contact": rows.personal.contact,
            "education": rows.personal.education,
            "experience": rows.personal.experience,
            "languages": rows.personal.languages,
            "learning_preferences": rows.personal.learning_preferences,
            "source_of_truth": rows.personal.source_of_truth,
        },
        "skills": {
            "general": general,
            "technical": [
                {
                    "id": item.id,
                    "label": item.label,
                    "declared_level": item.declared_level,
                    "assessed_level": item.assessed_level,
                    "confidence": item.confidence,
                    "evidence_card_ids": item.evidence_card_ids,
                    "last_evidence_at": item.last_evidence_at,
                    "details": item.details,
                }
                for item in rows.technical_skills
            ],
        },
        "career": {
            "goals": [
                {
                    "id": goal.id,
                    "title": goal.title,
                    "target_role": goal.target_role,
                    "target_date": goal.target_date,
                    "motivation": goal.motivation,
                    "status": goal.status,
                    "details": goal.details,
                }
                for goal in rows.goals
            ],
            "journeys": [
                {
                    "id": journey.id,
                    "career_goal_id": journey.career_goal_id,
                    "name": journey.name,
                    "status": journey.status,
                    "progress": journey.progress,
                    "plan": journey.plan,
                    "steps": [
                        {
                            "id": step.id,
                            "ord": step.ord,
                            "title": step.title,
                            "kind": step.kind,
                            "status": step.status,
                            "completed_at": step.completed_at,
                            "evidence_card_ids": step.evidence_card_ids,
                            "details": step.details,
                        }
                        for step in rows.steps_by_journey.get(journey.id, [])
                    ],
                }
                for journey in rows.journeys
            ],
        },
        "read_model": {
            "profile_version": profile.profile_version,
            "snapshot": profile.snapshot,
            "computed_at": profile.computed_at,
        },
    }
    return jsonable(state), jsonable(taxonomy)
