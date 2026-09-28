from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from learner_memory.db.models.learner import Learner, LearnerPersonalData, LearnerProfile
from learner_memory.db.repositories.base import Repository

# ON CONFLICT (id) absorbs a re-registration, so the only unique violation left
# on `learner` is uq_learner_org_external_id.
_UNIQUE_VIOLATION = "23505"


class ExternalIdTaken(Exception):
    """The external id is already registered to a different learner in this org."""

    def __init__(self, external_id: int) -> None:
        super().__init__(f"external_id {external_id} is already registered to another learner")
        self.external_id = external_id


class LearnerRepository(Repository[Learner]):
    model = Learner

    async def register(
        self,
        *,
        learner_id: uuid.UUID,
        external_id: int | None = None,
        display_name: str | None = None,
        program_id: uuid.UUID | None = None,
        cohort_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> tuple[Learner, bool]:
        """Idempotent registration on a *caller-supplied* id.

        The id comes from the upstream learn-os service and is stored verbatim as
        the primary key. Re-registering the same id updates the mutable fields and
        returns created=False, so a retried call is never an error.

        `external_id` is only ever set, never cleared: a re-registration that omits
        it keeps the stored value, so clients unaware of the field cannot erase it.

        Raises ExternalIdTaken if another learner in this org already holds
        `external_id`.
        """
        values = {
            "id": learner_id,
            "external_id": external_id,
            "organization_id": self.organization_id,
            "display_name": display_name,
            "program_id": program_id,
            "cohort_id": cohort_id,
            "status": "active",
            "registered_at": datetime.now(UTC),
            "metadata_": metadata or {},
        }
        insert_stmt = insert(Learner).values(**values)
        stmt = (
            insert_stmt
            .on_conflict_do_update(
                index_elements=[Learner.id],
                set_={
                    "external_id": func.coalesce(insert_stmt.excluded.external_id,
                                                 Learner.external_id),
                    "display_name": values["display_name"],
                    "program_id": values["program_id"],
                    "cohort_id": values["cohort_id"],
                    "updated_at": datetime.now(UTC),
                },
            )
            .returning(Learner, (Learner.registered_at == values["registered_at"]).label("created"))
        )
        try:
            row = (await self.session.execute(stmt)).first()
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION and external_id:
                raise ExternalIdTaken(external_id) from exc
            raise
        learner, created = row[0], bool(row[1])

        if created:
            # Every learner gets an empty profile + PII row up front, so reads
            # never have to special-case "registered but never processed".
            self.session.add(LearnerPersonalData(learner_id=learner_id))
            self.session.add(
                LearnerProfile(
                    learner_id=learner_id,
                    organization_id=self.organization_id,
                    snapshot={},
                    profile_version=0,
                )
            )
            await self.session.flush()
        return learner, created

    async def get_by_external_id(self, external_id: int) -> Learner | None:
        """The learner holding this LMS user id in this org, if registered."""
        res = await self.session.execute(self._scoped().where(Learner.external_id == external_id))
        return res.scalar_one_or_none()

    async def lock_by_external_id(self, external_id: int) -> Learner | None:
        """`get_by_external_id` with SELECT ... FOR UPDATE, held until the caller's
        transaction ends."""
        res = await self.session.execute(
            self._scoped().where(Learner.external_id == external_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def lock_personal_data(self, learner_id: uuid.UUID) -> LearnerPersonalData | None:
        """SELECT ... FOR UPDATE, held until the caller's transaction ends.

        The row has no organization column: callers pass an id they obtained
        through an org-scoped lookup.
        """
        res = await self.session.execute(
            select(LearnerPersonalData)
            .where(LearnerPersonalData.learner_id == learner_id)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def lock_profile(self, learner_id: uuid.UUID) -> LearnerProfile | None:
        """SELECT ... FOR UPDATE on the org's profile row, held until the caller's
        transaction ends."""
        res = await self.session.execute(
            select(LearnerProfile)
            .where(
                LearnerProfile.learner_id == learner_id,
                LearnerProfile.organization_id == self.organization_id,
            )
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def list_active(self, limit: int = 1000, offset: int = 0) -> list[Learner]:
        res = await self.session.execute(
            self._scoped().where(Learner.status == "active").limit(limit).offset(offset)
        )
        return list(res.scalars())

    async def get_profile(self, learner_id: uuid.UUID) -> LearnerProfile | None:
        """`learner_profile` is keyed on learner_id, not `id`, so the generic
        Repository.get does not apply. The org filter is what makes a
        cross-tenant read indistinguishable from a missing learner."""
        res = await self.session.execute(
            select(LearnerProfile).where(
                LearnerProfile.learner_id == learner_id,
                LearnerProfile.organization_id == self.organization_id,
            )
        )
        return res.scalar_one_or_none()
