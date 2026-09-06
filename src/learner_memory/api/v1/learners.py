"""Learner registration and lookup."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from learner_memory.api.deps import AuthDep, LearnerRepoDep
from learner_memory.core.logging import get_logger
from learner_memory.schemas.learner import LearnerRegisterRequest, LearnerResponse

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
    """
    auth.require("profile:write")
    learner, created = await repo.register(
        learner_id=body.id,
        external_user_id=body.external_user_id,
        display_name=body.display_name,
        program_id=body.program_id,
        cohort_id=body.cohort_id,
        metadata=body.metadata,
    )
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
