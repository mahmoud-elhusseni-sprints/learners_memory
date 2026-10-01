"""Learner registration and lookup."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from learner_memory.api.deps import AuthDep, LearnerRepoDep
from learner_memory.core.logging import get_logger
from learner_memory.db.repositories.learner import ExternalIdTaken
from learner_memory.schemas.learner import (
    LearnerProfileResponse,
    LearnerRegisterRequest,
    LearnerResponse,
)

router = APIRouter(prefix="/learners", tags=["learners"])
log = get_logger(__name__)


@router.post("", response_model=LearnerResponse, status_code=status.HTTP_201_CREATED)
async def register_learner(
    body: LearnerRegisterRequest,
    repo: LearnerRepoDep,
    auth: AuthDep,
    response: Response,
) -> LearnerResponse:
    """Register a learner under a caller-supplied id.

    The id in the body becomes the primary key verbatim, keeping learner ids
    identical across learn-os services. Idempotent: re-posting the same id
    refreshes the mutable fields and returns 200 instead of 201.

    `external_id` (the LMS user id) must be unique per organization; claiming one
    already held by a different learner is a 409.
    """
    auth.require("profile:write")
    try:
        learner, created = await repo.register(
            learner_id=body.id,
            external_id=body.external_id,
            display_name=body.display_name,
            program_id=body.program_id,
            cohort_id=body.cohort_id,
            metadata=body.metadata,
        )
    except ExternalIdTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    log.info("learner.registered", learner_id=str(body.id), created=created)
    return LearnerResponse(**LearnerResponse.model_validate(learner).model_dump(exclude={"created"}),
                           created=created)


@router.get("/{learner_id}", response_model=LearnerResponse)
async def get_learner(learner_id: uuid.UUID, repo: LearnerRepoDep, auth: AuthDep):
    auth.require("profile:read")
    learner = await repo.get(learner_id)
    if learner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "learner not found")
    return LearnerResponse.model_validate(learner)


@router.get("/{learner_id}/profile", response_model=LearnerProfileResponse)
async def get_learner_profile(learner_id: uuid.UUID, repo: LearnerRepoDep, auth: AuthDep):
    """Read model for remote services holding a `profile:read` API key.

    404 covers both 'no such learner' and 'learner in another org' — the repo's
    org filter makes them the same answer, so existence never leaks across tenants.
    """
    auth.require("profile:read")
    profile = await repo.get_profile(learner_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profile not found")
    log.info("profile.read", learner_id=str(learner_id), subject=auth.subject)
    return LearnerProfileResponse.model_validate(profile)
