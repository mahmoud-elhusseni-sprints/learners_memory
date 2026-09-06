from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from learner_memory.schemas.learner import LearnerRef
from learner_memory.schemas.memory_card import SourceType


class IngestRequest(BaseModel):
    """One envelope for every source — producers integrate once.

    `payload` is inline content (text or JSON); large binaries use the
    upload-url flow instead. `metadata` is passed through to the extractor's
    `build_context` untouched.
    """

    learner_ref: LearnerRef
    occurred_at: datetime = Field(description="When the evidence happened, not when sent")
    external_id: str | None = Field(None, description="Producer's id, for dedupe")
    payload: str | dict | None = None
    mime_type: str | None = None
    filename: str | None = None
    program_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _payload_required(self):
        if self.payload is None:
            raise ValueError("payload is required; use /upload-url for binary files")
        return self


class UploadUrlRequest(BaseModel):
    learner_ref: LearnerRef
    occurred_at: datetime
    external_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    program_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)


class UploadUrlResponse(BaseModel):
    document_id: uuid.UUID
    storage_key: str
    upload_url: str


class IngestResponse(BaseModel):
    document_id: uuid.UUID
    source_type: SourceType
    status: str
    duplicate: bool = Field(False, description="True when this content was already archived")
