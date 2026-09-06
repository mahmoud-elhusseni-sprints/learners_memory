from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class LearnerRegisterRequest(BaseModel):
    """The caller supplies the id. We store it as-is so learner ids are identical
    across every learn-os service — no local id, no mapping table, no drift."""

    id: uuid.UUID = Field(description="Learner id issued by the upstream learn-os service")
    external_user_id: str | None = None
    display_name: str | None = None
    program_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)


class LearnerRef(BaseModel):
    """Either identifier is accepted on ingest; both resolve to the same learner."""

    learner_id: uuid.UUID | None = None
    external_user_id: str | None = None

    @model_validator(mode="after")
    def _one_required(self):
        if not self.learner_id and not self.external_user_id:
            raise ValueError("learner_id or external_user_id is required")
        return self


class LearnerResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    external_user_id: str | None
    display_name: str | None
    status: str
    program_id: uuid.UUID | None
    cohort_id: uuid.UUID | None
    registered_at: datetime | None
    created: bool = Field(False, description="False when the learner already existed")

    model_config = {"from_attributes": True}
