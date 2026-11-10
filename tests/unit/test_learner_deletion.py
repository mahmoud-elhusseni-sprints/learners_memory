"""Soft-deleting a learner the LMS deleted.

An in-memory repository holds real ORM objects, so the assertions read the same
attributes the database would persist. Row locking needs Postgres and is not
covered here.
"""
from __future__ import annotations

import uuid

from learner_memory.db.models.learner import (
    Learner,
    LearnerPersonalData,
    LearnerProfile,
    LearnerStatus,
)
from learner_memory.services.external_sync import SyncOutcome
from learner_memory.services.learner_deletion import LearnerDeletion

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")
LMS_USER_ID = 47185


class InMemoryLearners:
    def __init__(self, learner, personal, profile):
        self.learner, self.personal, self.profile = learner, personal, profile

    async def lock_by_external_id(self, external_id):
        if self.learner is not None and self.learner.external_id == external_id:
            return self.learner
        return None

    async def reset_personal_data(self, learner_id):
        assert learner_id == self.learner.id
        self.personal = LearnerPersonalData(learner_id=learner_id)

    async def lock_profile(self, learner_id):
        return self.profile if self.profile and self.profile.learner_id == learner_id else None


def a_synced_learner(status: str = LearnerStatus.ACTIVE) -> InMemoryLearners:
    """A learner whose LMS profile and journey have been synced."""
    return InMemoryLearners(
        learner=Learner(id=LEARNER, organization_id=ORG, external_id=LMS_USER_ID,
                        display_name="Ramiro Carter", status=status),
        personal=LearnerPersonalData(learner_id=LEARNER, email="mervin.swift@example.org",
                                     phone="1001234567", location={"city": "Cairo"},
                                     github_id="4412"),
        profile=LearnerProfile(learner_id=LEARNER, organization_id=ORG, profile_version=5,
                               snapshot={"personal": {"email": "mervin.swift@example.org"},
                                         "career": {"journeys": [{"external_id": 9478}]},
                                         "skills": {"general": []}}),
    )


async def test_learner_is_marked_deleted_and_keeps_its_row():
    repo = a_synced_learner()

    outcome = await LearnerDeletion(repo).apply(LMS_USER_ID)

    assert outcome is SyncOutcome.APPLIED
    assert repo.learner.status == "deleted"
    assert (repo.learner.id, repo.learner.external_id) == (LEARNER, 47185)


async def test_personal_data_is_wiped():
    repo = a_synced_learner()

    await LearnerDeletion(repo).apply(LMS_USER_ID)

    assert repo.learner.display_name is None
    personal = repo.personal
    assert personal.learner_id == LEARNER
    assert (personal.email, personal.phone, personal.github_id) == (None, None, None)
    assert personal.location in (None, {})


async def test_snapshot_loses_only_its_personal_section():
    repo = a_synced_learner()

    await LearnerDeletion(repo).apply(LMS_USER_ID)

    assert repo.profile.snapshot == {"career": {"journeys": [{"external_id": 9478}]},
                                     "skills": {"general": []}}
    assert repo.profile.profile_version == 6


async def test_deleting_twice_changes_nothing_the_second_time():
    repo = a_synced_learner(status=LearnerStatus.DELETED)
    repo.profile.snapshot = {"skills": {"general": []}}

    outcome = await LearnerDeletion(repo).apply(LMS_USER_ID)

    assert outcome is SyncOutcome.LEARNER_DELETED
    assert repo.profile.profile_version == 5
    assert repo.personal.email == "mervin.swift@example.org"  # untouched by this call


async def test_unregistered_learner_is_unknown():
    repo = a_synced_learner()

    outcome = await LearnerDeletion(repo).apply(1)

    assert outcome is SyncOutcome.UNKNOWN_LEARNER
    assert repo.learner.status == "active"


async def test_learner_without_profile_row_is_still_deleted():
    repo = a_synced_learner()
    repo.profile = None

    outcome = await LearnerDeletion(repo).apply(LMS_USER_ID)

    assert outcome is SyncOutcome.APPLIED
    assert repo.learner.status == "deleted"
    assert repo.personal.email is None
