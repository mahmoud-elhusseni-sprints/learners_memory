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
