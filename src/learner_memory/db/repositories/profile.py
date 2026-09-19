"""Org-scoped persistence boundary for profile synthesis."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.db.models.learner import (
    CareerGoal,
    JourneyStep,
    Learner,
    LearnerPersonalData,
    LearnerProfile,
    LearnerTechnicalSkill,
    LearningJourney,
    SkillAssessment,
    SkillCatalog,
)
from learner_memory.db.models.raw import CardContribution, MemoryCardRecord
from learner_memory.db.repositories.base import Repository


@dataclass(frozen=True)
class ProfileStateRows:
    learner: Learner
    personal: LearnerPersonalData
    catalogs: list[SkillCatalog]
    assessments: dict[str, SkillAssessment]
    technical_skills: list[LearnerTechnicalSkill]
    goals: list[CareerGoal]
    journeys: list[LearningJourney]
    steps_by_journey: dict[uuid.UUID, list[JourneyStep]]


@dataclass(frozen=True)
class CardContributionVersion:
    """The exact contribution and card version supplied to profile synthesis."""

    card_id: uuid.UUID
    target: str
    key: str
    card_updated_at: datetime


class ProfileRepository(Repository[LearnerProfile]):
    """All database access needed by the L3 profile pipeline."""

    model = LearnerProfile

    async def get(self, id_: uuid.UUID) -> LearnerProfile | None:
        """LearnerProfile uses learner_id, rather than the generic ``id``, as its PK."""
        return await self.profile(id_)

    async def profile(
        self, learner_id: uuid.UUID, *, for_update: bool = False
    ) -> LearnerProfile | None:
        stmt = self._scoped().where(LearnerProfile.learner_id == learner_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_stale(self, learner_id: uuid.UUID, dimensions: list[str]) -> bool:
        profile = await self.profile(learner_id, for_update=True)
        if profile is None:
            return False
        profile.stale_dimensions = sorted(set(profile.stale_dimensions or []) | set(dimensions))
        return True

    @staticmethod
    async def stale_profiles(
        session: AsyncSession, batch_size: int
    ) -> list[tuple[uuid.UUID, uuid.UUID, list[str]]]:
        rows = (
            await session.execute(
                select(
                    LearnerProfile.organization_id,
                    LearnerProfile.learner_id,
                    LearnerProfile.stale_dimensions,
                )
                .where(LearnerProfile.stale_dimensions != [])
                .limit(batch_size)
            )
        ).all()
        return [(org_id, learner_id, list(dimensions)) for org_id, learner_id, dimensions in rows]

    async def state_rows(self, learner_id: uuid.UUID) -> ProfileStateRows:
        learner = (
            await self.session.execute(
                select(Learner).where(
                    Learner.id == learner_id,
                    Learner.organization_id == self.organization_id,
                )
            )
        ).scalar_one()
        personal = (
            await self.session.execute(
                select(LearnerPersonalData).where(
                    LearnerPersonalData.learner_id == learner_id
                )
            )
        ).scalar_one()
        catalogs = list(
            (
                await self.session.execute(
                    select(SkillCatalog)
                    .where(SkillCatalog.active.is_(True))
                    .order_by(SkillCatalog.slug)
                )
            ).scalars()
        )
        assessment_rows = (
            await self.session.execute(
                select(SkillAssessment, SkillCatalog)
                .join(SkillCatalog, SkillCatalog.id == SkillAssessment.skill_id)
                .where(
                    SkillAssessment.organization_id == self.organization_id,
                    SkillAssessment.learner_id == learner_id,
                    SkillAssessment.superseded.is_(False),
                )
            )
        ).all()
        assessments = {catalog.slug: assessment for assessment, catalog in assessment_rows}
        technical_skills = list(
            (
                await self.session.execute(
                    select(LearnerTechnicalSkill)
                    .where(LearnerTechnicalSkill.learner_id == learner_id)
                    .order_by(LearnerTechnicalSkill.label)
                )
            ).scalars()
        )
        goals = list(
            (
                await self.session.execute(
                    select(CareerGoal)
                    .where(CareerGoal.learner_id == learner_id)
                    .order_by(CareerGoal.created_at)
                )
            ).scalars()
        )
        journeys = list(
            (
                await self.session.execute(
                    select(LearningJourney)
                    .where(LearningJourney.learner_id == learner_id)
                    .order_by(LearningJourney.created_at)
                )
            ).scalars()
        )
        steps_by_journey: dict[uuid.UUID, list[JourneyStep]] = {}
        journey_ids = [journey.id for journey in journeys]
        if journey_ids:
            steps = list(
                (
                    await self.session.execute(
                        select(JourneyStep)
                        .where(JourneyStep.journey_id.in_(journey_ids))
                        .order_by(JourneyStep.journey_id, JourneyStep.ord)
                    )
                ).scalars()
            )
            for step in steps:
                steps_by_journey.setdefault(step.journey_id, []).append(step)
        return ProfileStateRows(
            learner=learner,
            personal=personal,
            catalogs=catalogs,
            assessments=assessments,
            technical_skills=technical_skills,
            goals=goals,
            journeys=journeys,
            steps_by_journey=steps_by_journey,
        )

    async def fresh_card_contributions(
        self,
        *,
        learner_id: uuid.UUID,
        dimensions: list[tuple[str, str]],
        since: datetime,
        now: datetime,
    ) -> list[tuple[MemoryCardRecord, CardContribution]]:
        conditions = [
            and_(CardContribution.target == target, CardContribution.key == key)
            for target, key in dimensions
        ]
        result = await self.session.execute(
            select(MemoryCardRecord, CardContribution)
            .join(CardContribution, CardContribution.card_id == MemoryCardRecord.id)
            .where(
                *self._fresh_contribution_conditions(
                    learner_id=learner_id,
                    conditions=conditions,
                    since=since,
                    now=now,
                )
            )
            .order_by(MemoryCardRecord.observed_at.desc())
        )
        return list(result.tuples())

    async def mark_card_contributions_consumed(
        self, versions: list[CardContributionVersion]
    ) -> None:
        """Mark only the exact card versions that were supplied to the agent."""
        if not versions:
            return
        identities = [
            and_(
                CardContribution.card_id == version.card_id,
                CardContribution.target == version.target,
                CardContribution.key == version.key,
            )
            for version in versions
        ]
        rows = list(
            (
                await self.session.execute(
                    select(CardContribution)
                    .join(MemoryCardRecord, MemoryCardRecord.id == CardContribution.card_id)
                    .where(
                        MemoryCardRecord.organization_id == self.organization_id,
                        or_(*identities),
                    )
                )
            ).scalars()
        )
        version_by_identity = {
            (version.card_id, version.target, version.key): version.card_updated_at
            for version in versions
        }
        for row in rows:
            card_updated_at = version_by_identity[(row.card_id, row.target, row.key)]
            if (
                row.profile_consumed_card_updated_at is None
                or row.profile_consumed_card_updated_at < card_updated_at
            ):
                row.profile_consumed_card_updated_at = card_updated_at

    async def dimensions_with_fresh_contributions(
        self,
        *,
        learner_id: uuid.UUID,
        dimensions: list[tuple[str, str]],
        since: datetime,
        now: datetime,
    ) -> set[str]:
        conditions = [
            and_(CardContribution.target == target, CardContribution.key == key)
            for target, key in dimensions
        ]
        rows = (
            await self.session.execute(
                select(CardContribution.target, CardContribution.key)
                .join(MemoryCardRecord, MemoryCardRecord.id == CardContribution.card_id)
                .where(
                    *self._fresh_contribution_conditions(
                        learner_id=learner_id,
                        conditions=conditions,
                        since=since,
                        now=now,
                    )
                )
                .distinct()
            )
        ).all()
        return {f"{target}:{key}" for target, key in rows}

    def _fresh_contribution_conditions(
        self,
        *,
        learner_id: uuid.UUID,
        conditions: list[Any],
        since: datetime,
        now: datetime,
    ) -> list[Any]:
        return [
            MemoryCardRecord.organization_id == self.organization_id,
            MemoryCardRecord.learner_id == learner_id,
            MemoryCardRecord.status == "indexed",
            MemoryCardRecord.superseded_by.is_(None),
            MemoryCardRecord.observed_at >= since,
            or_(MemoryCardRecord.valid_until.is_(None), MemoryCardRecord.valid_until > now),
            or_(
                CardContribution.profile_consumed_card_updated_at.is_(None),
                CardContribution.profile_consumed_card_updated_at
                < MemoryCardRecord.updated_at,
            ),
            or_(*conditions),
        ]

    async def skill_catalog(self, slug: str) -> SkillCatalog | None:
        return (
            await self.session.execute(
                select(SkillCatalog).where(
                    SkillCatalog.slug == slug,
                    SkillCatalog.active.is_(True),
                )
            )
        ).scalar_one_or_none()

    async def current_assessment(
        self, learner_id: uuid.UUID, skill_id: uuid.UUID
    ) -> SkillAssessment | None:
        return (
            await self.session.execute(
                select(SkillAssessment).where(
                    SkillAssessment.organization_id == self.organization_id,
                    SkillAssessment.learner_id == learner_id,
                    SkillAssessment.skill_id == skill_id,
                    SkillAssessment.superseded.is_(False),
                )
            )
        ).scalar_one_or_none()

    async def technical_skills(self, learner_id: uuid.UUID) -> list[LearnerTechnicalSkill]:
        return list(
            (
                await self.session.execute(
                    select(LearnerTechnicalSkill).where(
                        LearnerTechnicalSkill.learner_id == learner_id
                    )
                )
            ).scalars()
        )

    async def personal_data(self, learner_id: uuid.UUID) -> LearnerPersonalData:
        return (
            await self.session.execute(
                select(LearnerPersonalData).where(
                    LearnerPersonalData.learner_id == learner_id
                )
            )
        ).scalar_one()

    async def career_goal(
        self, entity_id: uuid.UUID, learner_id: uuid.UUID
    ) -> CareerGoal | None:
        return (
            await self.session.execute(
                select(CareerGoal).where(
                    CareerGoal.id == entity_id,
                    CareerGoal.learner_id == learner_id,
                )
            )
        ).scalar_one_or_none()

    async def journey(
        self, entity_id: uuid.UUID, learner_id: uuid.UUID
    ) -> LearningJourney | None:
        return (
            await self.session.execute(
                select(LearningJourney).where(
                    LearningJourney.id == entity_id,
                    LearningJourney.learner_id == learner_id,
                )
            )
        ).scalar_one_or_none()

    async def journey_step(
        self, entity_id: uuid.UUID, learner_id: uuid.UUID
    ) -> JourneyStep | None:
        return (
            await self.session.execute(
                select(JourneyStep)
                .join(LearningJourney, LearningJourney.id == JourneyStep.journey_id)
                .where(
                    JourneyStep.id == entity_id,
                    LearningJourney.learner_id == learner_id,
                )
            )
        ).scalar_one_or_none()

    async def journeys(self, learner_id: uuid.UUID) -> list[LearningJourney]:
        return list(
            (
                await self.session.execute(
                    select(LearningJourney).where(
                        LearningJourney.learner_id == learner_id
                    )
                )
            ).scalars()
        )

    async def journey_steps(self, journey_id: uuid.UUID) -> list[JourneyStep]:
        return list(
            (
                await self.session.execute(
                    select(JourneyStep).where(JourneyStep.journey_id == journey_id)
                )
            ).scalars()
        )

    def store(self, entity: Any) -> None:
        self.session.add(entity)

    async def flush(self) -> None:
        await self.session.flush()
