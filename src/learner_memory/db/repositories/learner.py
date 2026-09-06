from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from learner_memory.db.models.learner import Learner, LearnerPersonalData, LearnerProfile
from learner_memory.db.repositories.base import Repository


class LearnerRepository(Repository[Learner]):
    model = Learner

    async def register(
        self,
        *,
        learner_id: uuid.UUID,
        display_name: str | None = None,
        program_id: uuid.UUID | None = None,
        cohort_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> tuple[Learner, bool]:
        """Idempotent registration on a *caller-supplied* id.

        The id comes from the upstream learn-os service and is stored verbatim as
        the primary key. Re-registering the same id updates the mutable fields and
        returns created=False, so a retried call is never an error.
        """
        values = {
            "id": learner_id,
            "organization_id": self.organization_id,
            "display_name": display_name,
            "program_id": program_id,
            "cohort_id": cohort_id,
            "status": "active",
            "registered_at": datetime.now(UTC),
            "metadata": metadata or {},
        }
        stmt = (
            insert(Learner)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[Learner.id],
                set_={
                    "display_name": values["display_name"],
                    "program_id": values["program_id"],
                    "cohort_id": values["cohort_id"],
                    "updated_at": datetime.now(UTC),
                },
            )
            .returning(Learner, (Learner.registered_at == values["registered_at"]).label("created"))
        )
        row = (await self.session.execute(stmt)).first()
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

    async def list_active(self, limit: int = 1000, offset: int = 0) -> list[Learner]:
        res = await self.session.execute(
            self._scoped().where(Learner.status == "active").limit(limit).offset(offset)
        )
        return list(res.scalars())
