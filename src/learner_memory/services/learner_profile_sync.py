"""Apply a learner's LMS personal information to their profile.

These fields change the profile directly: no memory cards and no synthesis. The
LMS is the system of record for them, so every sync mirrors its current values
(an LMS null clears ours). The one exception is `learner.display_name`, which the
registering service sets and an empty LMS name leaves alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from learner_memory.db.models.learner import LearnerPersonalData
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.services.external_sync import SyncOutcome, is_stale

PERSONAL_SECTION = "personal"


@dataclass(frozen=True, slots=True)
class PersonalInfoUpdate:
    """The learner's personal information as the LMS currently holds it.

    `source_updated_at` is the LMS's own change time, used to reject stale reads.
    """

    full_name: str | None
    email: str | None
    phone: str | None
    phone_country_code: str | None
    city: str | None
    country: str | None
    bio: str | None
    avatar_url: str | None
    timezone: str | None
    preferred_language: str | None
    job_preference: str | None
    github_url: str | None
    linkedin_url: str | None
    cv_url: str | None
    github_id: str | None
    linkedin_id: str | None
    source_updated_at: datetime | None


class LearnerProfileSync:
    def __init__(self, learners: LearnerRepository) -> None:
        self._learners = learners

    async def apply(self, external_id: int, update: PersonalInfoUpdate) -> SyncOutcome:
        """Write `update` to the learner holding `external_id`, within the caller's
        transaction. Row locks serialize concurrent syncs for the same learner.

        Raises RuntimeError if the learner lacks the personal-data or profile row
        that registration always creates.
        """
        learner = await self._learners.get_by_external_id(external_id)
        if learner is None:
            return SyncOutcome.UNKNOWN_LEARNER

        personal = await self._learners.lock_personal_data(learner.id)
        profile = await self._learners.lock_profile(learner.id)
        if personal is None or profile is None:
            raise RuntimeError(
                f"learner {learner.id} is missing its personal-data or profile row; "
                "re-register the learner to recreate them"
            )
        if is_stale(personal.external_updated_at, update.source_updated_at):
            return SyncOutcome.STALE

        if update.full_name:
            learner.display_name = update.full_name
        _write_personal(personal, update)
        # Reassign rather than mutate: JSONB changes are only tracked on assignment.
        profile.snapshot = {**profile.snapshot, PERSONAL_SECTION: personal_section(personal)}
        profile.profile_version += 1
        return SyncOutcome.APPLIED


def _write_personal(personal: LearnerPersonalData, update: PersonalInfoUpdate) -> None:
    personal.email = update.email
    personal.phone = update.phone
    personal.phone_country_code = update.phone_country_code
    personal.location = {"city": update.city, "country": update.country}
    personal.bio = update.bio
    personal.avatar_url = update.avatar_url
    personal.timezone = update.timezone
    personal.preferred_language = update.preferred_language
    personal.job_preference = update.job_preference
    personal.github_url = update.github_url
    personal.linkedin_url = update.linkedin_url
    personal.cv_url = update.cv_url
    personal.github_id = update.github_id
    personal.linkedin_id = update.linkedin_id
    personal.external_updated_at = update.source_updated_at


def personal_section(personal: LearnerPersonalData) -> dict:
    """The profile snapshot's `personal` section, rebuilt from the whole row so a
    field written by another path is never dropped."""
    return {
        "email": personal.email,
        "phone": personal.phone,
        "phone_country_code": personal.phone_country_code,
        "location": personal.location,
        "bio": personal.bio,
        "avatar_url": personal.avatar_url,
        "timezone": personal.timezone,
        "preferred_language": personal.preferred_language,
        "job_preference": personal.job_preference,
        "links": {
            "github": personal.github_url,
            "linkedin": personal.linkedin_url,
            "cv": personal.cv_url,
        },
        "github_id": personal.github_id,
        "linkedin_id": personal.linkedin_id,
        "contact": personal.contact,
        "education": personal.education,
        "experience": personal.experience,
        "languages": personal.languages,
        "learning_preferences": personal.learning_preferences,
    }
