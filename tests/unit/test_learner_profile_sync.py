"""Applying LMS personal info to a learner.

An in-memory repository holds real ORM objects, so the assertions read the same
attributes the database would persist. Row locking needs Postgres and is not
covered here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from learner_memory.db.models.learner import Learner, LearnerPersonalData, LearnerProfile
from learner_memory.services.learner_profile_sync import (
    LearnerProfileSync,
    PersonalInfoUpdate,
    SyncOutcome,
)

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 90232436
APPLIED_AT = datetime(2026, 9, 16, 2, 18, 5, tzinfo=UTC)


class InMemoryLearners:
    def __init__(self, learner=None, personal=None, profile=None):
        self.learner, self.personal, self.profile = learner, personal, profile

    async def get_by_external_id(self, external_id):
        if self.learner is not None and self.learner.external_id == external_id:
            return self.learner
        return None

    async def lock_personal_data(self, learner_id):
        return self.personal if self.personal and self.personal.learner_id == learner_id else None

    async def lock_profile(self, learner_id):
        return self.profile if self.profile and self.profile.learner_id == learner_id else None


def registered(**personal_overrides) -> InMemoryLearners:
    """A learner as registration leaves them: empty personal data, empty profile."""
    personal = {
        "learner_id": LEARNER, "email": None, "phone": None, "location": {}, "contact": {},
        "education": [], "experience": [], "languages": [], "learning_preferences": {},
        "external_updated_at": None,
    }
    personal.update(personal_overrides)
    return InMemoryLearners(
        learner=Learner(id=LEARNER, organization_id=ORG, external_id=LMS_USER_ID,
                        display_name="Registered Name"),
        personal=LearnerPersonalData(**personal),
        profile=LearnerProfile(learner_id=LEARNER, organization_id=ORG,
                               snapshot={"skills": {"general": []}}, profile_version=3),
    )


def an_update(**overrides) -> PersonalInfoUpdate:
    fields = {
        "full_name": "Ardith Conn", "email": "mreynolds@example.com", "phone": "1001234567",
        "phone_country_code": "+20", "city": "Cairo", "country": "Egypt",
        "bio": "Backend developer", "avatar_url": "https://cdn.test/a.png",
        "timezone": "Africa/Cairo", "preferred_language": "en", "job_preference": "Onsite",
        "github_url": "https://github.com/ardith", "linkedin_url": None, "cv_url": None,
        "github_id": "4412", "linkedin_id": None, "source_updated_at": APPLIED_AT,
    }
    fields.update(overrides)
    return PersonalInfoUpdate(**fields)


async def test_applies_personal_info_to_the_learner():
    repo = registered()

    outcome = await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update())

    assert outcome is SyncOutcome.APPLIED
    personal = repo.personal
    assert repo.learner.display_name == "Ardith Conn"
    assert (personal.email, personal.phone, personal.phone_country_code) == (
        "mreynolds@example.com", "1001234567", "+20")
    assert personal.location == {"city": "Cairo", "country": "Egypt"}
    assert (personal.bio, personal.avatar_url) == ("Backend developer", "https://cdn.test/a.png")
    assert (personal.timezone, personal.preferred_language) == ("Africa/Cairo", "en")
    assert personal.job_preference == "Onsite"
    assert (personal.github_url, personal.github_id) == ("https://github.com/ardith", "4412")
    assert personal.external_updated_at == APPLIED_AT


async def test_profile_snapshot_gets_the_personal_section_and_a_new_version():
    repo = registered()

    await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update())

    snapshot = repo.profile.snapshot
    assert repo.profile.profile_version == 4
    assert snapshot["skills"] == {"general": []}
    assert snapshot["personal"]["email"] == "mreynolds@example.com"
    assert snapshot["personal"]["location"] == {"city": "Cairo", "country": "Egypt"}
    assert snapshot["personal"]["links"] == {
        "github": "https://github.com/ardith", "linkedin": None, "cv": None}
    assert snapshot["personal"]["education"] == []


async def test_lms_null_clears_the_stored_value():
    repo = registered(bio="Old bio", email="old@example.com")

    await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update(bio=None))

    assert repo.personal.bio is None
    assert repo.profile.snapshot["personal"]["bio"] is None


async def test_missing_lms_name_keeps_the_registered_display_name():
    repo = registered()

    await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update(full_name=None))

    assert repo.learner.display_name == "Registered Name"


async def test_older_lms_data_does_not_overwrite_newer():
    newer = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
    repo = registered(email="newer@example.com", external_updated_at=newer)

    outcome = await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update())

    assert outcome is SyncOutcome.STALE
    assert repo.personal.email == "newer@example.com"
    assert repo.profile.profile_version == 3


async def test_same_second_lms_data_is_applied():
    repo = registered(email="first@example.com", external_updated_at=APPLIED_AT)

    outcome = await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update())

    assert outcome is SyncOutcome.APPLIED
    assert repo.personal.email == "mreynolds@example.com"


async def test_lms_data_without_a_timestamp_is_applied():
    repo = registered(external_updated_at=APPLIED_AT)

    outcome = await LearnerProfileSync(repo).apply(
        LMS_USER_ID, an_update(source_updated_at=None))

    assert outcome is SyncOutcome.APPLIED


async def test_unregistered_external_id_changes_nothing():
    repo = registered()

    outcome = await LearnerProfileSync(repo).apply(1, an_update())

    assert outcome is SyncOutcome.UNKNOWN_LEARNER
    assert repo.personal.email is None


async def test_learner_without_profile_row_is_an_error():
    repo = registered()
    repo.profile = None

    with pytest.raises(RuntimeError, match="missing its personal-data or profile row"):
        await LearnerProfileSync(repo).apply(LMS_USER_ID, an_update())

