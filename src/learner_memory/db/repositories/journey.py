"""Learning journeys and their steps.

These tables carry no organization column: every method takes a learner or
journey id that the caller obtained through an org-scoped lookup
(LearnerRepository), never one taken from a request.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.db.models.learner import JourneyStep, LearningJourney


class JourneyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_if_missing(self, learner_id: uuid.UUID, external_id: int,
                                status: str) -> None:
        """Insert the learner's journey for this LMS id unless it exists.

        ON CONFLICT DO NOTHING makes two first syncs for the same journey safe:
        one inserts, the other finds the row when it locks it.
        """
        await self.session.execute(
            insert(LearningJourney)
            .values(learner_id=learner_id, external_id=external_id, status=status)
            .on_conflict_do_nothing(constraint="uq_learning_journey_learner_external_id")
        )

    async def lock_by_external_id(self, learner_id: uuid.UUID,
                                  external_id: int) -> LearningJourney | None:
        """SELECT ... FOR UPDATE, held until the caller's transaction ends. The
        lock also serializes step creation for the journey."""
        res = await self.session.execute(
            select(LearningJourney)
            .where(LearningJourney.learner_id == learner_id,
                   LearningJourney.external_id == external_id)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def list_steps(self, journey_ids: list[uuid.UUID]) -> list[JourneyStep]:
        if not journey_ids:
            return []
        res = await self.session.execute(
            select(JourneyStep)
            .where(JourneyStep.journey_id.in_(journey_ids))
            .order_by(JourneyStep.ord, JourneyStep.external_id)
        )
        return list(res.scalars())

    def add_step(self, step: JourneyStep) -> None:
        self.session.add(step)

    async def list_for_learner(self, learner_id: uuid.UUID) -> list[LearningJourney]:
        """Every journey of the learner, earliest start first. A learner holds a
        handful, so this is not paginated."""
        res = await self.session.execute(
            select(LearningJourney)
            .where(LearningJourney.learner_id == learner_id)
            .order_by(LearningJourney.started_at.asc().nulls_last(), LearningJourney.created_at)
        )
        return list(res.scalars())
