from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class LearnerRegisterRequest(BaseModel):
    """The caller supplies the id. We store it as-is so learner ids are identical
    across every learn-os service — no local id, no mapping table, no drift."""

    id: uuid.UUID = Field(description="Learner id issued by the upstream learn-os service")
    display_name: str | None = None
    program_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)


class LearnerResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    display_name: str | None
    status: str
    program_id: uuid.UUID | None
    cohort_id: uuid.UUID | None
    registered_at: datetime | None
    created: bool = Field(False, description="False when the learner already existed")

    model_config = {"from_attributes": True}


class LearnerProfileResponse(BaseModel):
    """The `learner_profile` read model, verbatim.

    `stale_dimensions` is exposed rather than hidden: it tells a remote service
    the snapshot is mid-recompute, so it can decide whether to cache the result.
    """

    learner_id: uuid.UUID
    organization_id: uuid.UUID
    profile_version: int
    computed_at: datetime | None
    stale_dimensions: list[str] = Field(default_factory=list)
    snapshot: dict = Field(default_factory=dict)

    model_config = {"from_attributes": True}
