"""Soft-delete a learner the LMS deleted.

The learner row, its memory and its journeys stay; `status` becomes `deleted`.
Personal data does not stay: the PII row is replaced with an empty one,
`display_name` is cleared, and the profile snapshot loses its `personal`
section. Every LMS sync skips a deleted learner, so nothing writes it back.
"""
from __future__ import annotations

import uuid

from learner_memory.db.models.learner import LearnerStatus
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.services.external_sync import SyncOutcome
from learner_memory.services.learner_profile_sync import PERSONAL_SECTION


class LearnerDeletion:
    def __init__(self, learners: LearnerRepository) -> None:
        self._learners = learners

    async def apply(self, external_id: int) -> SyncOutcome:
        """Soft-delete the learner holding `external_id`, within the caller's
        transaction. The learner row lock is taken first, as every sync does.

        UNKNOWN_LEARNER if no learner holds the id; LEARNER_DELETED (a no-op) if
        the learner was already deleted.
        """
        learner = await self._learners.lock_by_external_id(external_id)
        if learner is None:
            return SyncOutcome.UNKNOWN_LEARNER
        if learner.status == LearnerStatus.DELETED:
            return SyncOutcome.LEARNER_DELETED

        learner.status = LearnerStatus.DELETED
        learner.display_name = None
        await self._learners.reset_personal_data(learner.id)
        await self._drop_personal_section(learner.id)
        return SyncOutcome.APPLIED

    async def _drop_personal_section(self, learner_id: uuid.UUID) -> None:
        profile = await self._learners.lock_profile(learner_id)
        if profile is None:
            return
        # Reassign rather than mutate: JSONB changes are only tracked on assignment.
        profile.snapshot = {section: value for section, value in profile.snapshot.items()
                            if section != PERSONAL_SECTION}
        profile.profile_version += 1
