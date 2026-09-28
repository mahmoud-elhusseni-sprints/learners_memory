"""Mirror a learner's LMS journey enrollment onto `learner.external_journey_ids`.

Each sync reads one journey's enrollment as the LMS holds it now and adds or
removes that one id, so syncs for different journeys never undo each other and
a replayed or reordered sync lands on the same set. The learner row is locked,
so two syncs for the same learner cannot overwrite each other's change.
"""
from __future__ import annotations

from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.services.learner_profile_sync import SyncOutcome


class LearnerJourneySync:
    def __init__(self, learners: LearnerRepository) -> None:
        self._learners = learners

    async def apply(self, external_id: int, journey_id: int, enrolled: bool) -> SyncOutcome:
        """Record whether the learner holding `external_id` is enrolled in
        `journey_id`, within the caller's transaction."""
        learner = await self._learners.lock_by_external_id(external_id)
        if learner is None:
            return SyncOutcome.UNKNOWN_LEARNER

        current = learner.external_journey_ids or []
        if enrolled == (journey_id in current):
            return SyncOutcome.UNCHANGED
        # Reassign rather than mutate: ARRAY changes are only tracked on assignment.
        learner.external_journey_ids = (
            sorted({*current, journey_id}) if enrolled
            else [i for i in current if i != journey_id]
        )
        return SyncOutcome.APPLIED
