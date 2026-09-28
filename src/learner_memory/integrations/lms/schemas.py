"""LMS shapes at the boundary: the learner webhook envelope, and the parts of the
learner context response we use. Each sync reads its own `include` of the context
endpoint, so each has its own `LmsContext` subclass.

The context response also carries financial and identity fields (IBAN, bank and
wallet details, identity scans). Nothing here declares them, so they are dropped
at parse time and never reach storage or logs.

Response keys are required but nullable: an explicit null clears the stored
value, while a missing key is a contract break that fails the sync instead of
silently wiping a field.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Generic, Literal, TypeVar
from urllib.parse import parse_qs

from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, Field

from learner_memory.schemas.learner import BIGINT_MAX

_URL_MAX = 2048
_BIO_MAX = 10_000
_PLACE_MAX = 128

# The LMS has sent these ids as null only so far; accept either JSON type.
_ExternalAccountId = Annotated[str, Field(max_length=255)] | int


class LmsLearnerEvent(BaseModel):
    """The webhook body. It only says *that* something changed; the data itself
    is read back from the LMS."""

    event: Literal["content.created", "content.updated"]
    event_id: uuid.UUID
    entity_type: Literal["learner"]
    user_id: int = Field(gt=0, le=BIGINT_MAX)
    api_url: AnyHttpUrl
    occurred_at: AwareDatetime
    # Present only on events scoped to one journey or one form submission.
    journey_id: int | None = Field(None, gt=0, le=BIGINT_MAX)
    form_id: int | None = Field(None, gt=0, le=BIGINT_MAX)

    def includes(self) -> frozenset[str]:
        """The resources `api_url` asks for, e.g. {"profile"} or {"enrollments", "progress"}.

        Only used to decide which sync to run. The URL itself is never requested.
        """
        values = parse_qs(self.api_url.query or "").get("include", [])
        return frozenset(part for value in values for part in value.split(",") if part)


class LmsContact(BaseModel):
    country_code: str | None = Field(max_length=8)
    mobile_number: str | None = Field(max_length=64)


class LmsAvatar(BaseModel):
    cdn_url: str | None = Field(max_length=_URL_MAX)


class LmsMedia(BaseModel):
    avatar: LmsAvatar


class LmsBasicInfo(BaseModel):
    email: str | None = Field(max_length=320)
    full_name: str | None = Field(max_length=255)
    bio: str | None = Field(max_length=_BIO_MAX)
    contact: LmsContact
    media: LmsMedia
    timezone: str | None = Field(max_length=64)
    language: str | None = Field(max_length=16)


class LmsLocation(BaseModel):
    country: str | None = Field(max_length=_PLACE_MAX)
    city: str | None = Field(max_length=_PLACE_MAX)


class LmsLinks(BaseModel):
    github: str | None = Field(max_length=_URL_MAX)
    linkedin: str | None = Field(max_length=_URL_MAX)
    cv: str | None = Field(max_length=_URL_MAX)


class LmsProfileFields(BaseModel):
    """The raw user row. Only the values `basic_info`, `location` and `links` lack."""

    github_id: _ExternalAccountId | None
    linkedin_id: _ExternalAccountId | None
    job_preference: str | None = Field(max_length=32)


class LmsProfile(BaseModel):
    fields: LmsProfileFields
    basic_info: LmsBasicInfo
    location: LmsLocation
    links: LmsLinks


class LmsContext(BaseModel):
    """What every include of the context endpoint answers with."""

    user_id: int
    last_updated_at: AwareDatetime | None


class LmsProfileContext(LmsContext):
    """`include=profile`."""

    profile: LmsProfile


class LmsEnrollment(BaseModel):
    journey_id: int
    status: str | None = Field(max_length=32)
    blocked: bool


class LmsJourneyContext(LmsContext):
    """`include=enrollments,progress`, narrowed by `journey_id`. Only enrollment is
    kept, so `progress` is not declared and is dropped at parse time."""

    enrollments: list[LmsEnrollment]


ContextT = TypeVar("ContextT", bound=LmsContext)


class LmsContextEnvelope(BaseModel, Generic[ContextT]):
    success: bool
    data: ContextT | None = None
