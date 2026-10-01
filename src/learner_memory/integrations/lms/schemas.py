"""LMS shapes at the boundary: the learner webhook envelope, and the part of the
learner context response we use.

The context response also carries financial and identity fields (IBAN, bank and
wallet details, identity scans). Nothing here declares them, so they are dropped
at parse time and never reach storage or logs.

Response keys are required but nullable: an explicit null clears the stored
value, while a missing key is a contract break that fails the sync instead of
silently wiping a field.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar
from urllib.parse import parse_qs

from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, Field, model_validator

from learner_memory.schemas.learner import BIGINT_MAX

PROFILE_INCLUDE = "profile"
JOURNEY_INCLUDES = ("enrollments", "progress")


class LmsResource(StrEnum):
    """What a webhook says changed; each resource has its own sync."""

    PROFILE = "profile"
    JOURNEY = "journey"


# Checked in order: the first resource whose includes `api_url` all requests wins.
_RESOURCE_INCLUDES: tuple[tuple[LmsResource, frozenset[str]], ...] = (
    (LmsResource.PROFILE, frozenset({PROFILE_INCLUDE})),
    (LmsResource.JOURNEY, frozenset(JOURNEY_INCLUDES)),
)

_URL_MAX = 2048
_BIO_MAX = 10_000
_PLACE_MAX = 128
_JOURNEYS_MAX = 100     # per response; the LMS filters to one journey
_PROGRAMS_MAX = 500     # per journey

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
    journey_id: int | None = Field(default=None, gt=0, le=BIGINT_MAX)

    @model_validator(mode="after")
    def _journey_events_name_their_journey(self) -> LmsLearnerEvent:
        if self.resource() is LmsResource.JOURNEY and self.journey_id is None:
            raise ValueError("journey events (include=enrollments,progress) need journey_id")
        return self

    def includes(self) -> frozenset[str]:
        """The resources `api_url` asks for, e.g. {"profile"} or {"enrollments", "progress"}.

        Only used to decide which sync to run. The URL itself is never requested.
        """
        values = parse_qs(self.api_url.query or "").get("include", [])
        return frozenset(part for value in values for part in value.split(",") if part)

    def resource(self) -> LmsResource | None:
        """The resource this event is about, or None for one we do not sync."""
        includes = self.includes()
        return next((res for res, needed in _RESOURCE_INCLUDES if needed <= includes), None)


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


class LmsContextData(BaseModel):
    """What every learner context response carries, whatever it includes."""

    user_id: int


class LmsProfileContext(LmsContextData):
    last_updated_at: AwareDatetime | None
    profile: LmsProfile


class LmsEnrollment(BaseModel):
    journey_id: int
    journey_slug: str | None = Field(max_length=255)
    status: str = Field(min_length=1, max_length=32)
    blocked: bool
    manual_added: bool
    start_date: AwareDatetime | None
    graduation_date: AwareDatetime | None
    last_updated_at: AwareDatetime | None
    public_url: str | None = Field(max_length=_URL_MAX)


class LmsProgramCounters(BaseModel):
    """Items of each kind the learner has completed in the program."""

    files_counter: int = Field(ge=0)
    videos_counter: int = Field(ge=0)
    text_lessons_counter: int = Field(ge=0)
    sessions_live_counter: int = Field(ge=0)
    sessions_record_counter: int = Field(ge=0)
    codelabs_counter: int = Field(ge=0)
    quizzes_counter: int = Field(ge=0)
    tasks_counter: int = Field(ge=0)
    regular_projects_counter: int = Field(ge=0)
    final_projects_counter: int = Field(ge=0)
    peer_reviews_counter: int = Field(ge=0)
    ai_interviews_counter: int = Field(ge=0)


class LmsProgramProgress(BaseModel):
    program_id: int = Field(gt=0, le=BIGINT_MAX)
    counters: LmsProgramCounters


class LmsJourneyProgress(BaseModel):
    journey_id: int
    progress_percent: Decimal = Field(ge=0, le=100)
    last_updated_at: AwareDatetime | None
    programs: list[LmsProgramProgress] = Field(max_length=_PROGRAMS_MAX)


class LmsJourneyContext(LmsContextData):
    """Enrollments and progress, filtered by the LMS to the requested journey."""

    enrollments: list[LmsEnrollment] = Field(max_length=_JOURNEYS_MAX)
    progress: list[LmsJourneyProgress] = Field(max_length=_JOURNEYS_MAX)


ContextT = TypeVar("ContextT", bound=LmsContextData)


class LmsEnvelope(BaseModel, Generic[ContextT]):
    success: bool
    data: ContextT | None = None


LmsProfileEnvelope = LmsEnvelope[LmsProfileContext]
LmsJourneyEnvelope = LmsEnvelope[LmsJourneyContext]
