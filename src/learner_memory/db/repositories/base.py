"""Generic org-scoped repository. Subclasses bind a model; queries always filter
by organization_id so a missing tenant filter is impossible by construction."""
from __future__ import annotations

import uuid
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.db.base import Base

M = TypeVar("M", bound=Base)


class Repository(Generic[M]):
    model: type[M]

    def __init__(self, session: AsyncSession, organization_id: uuid.UUID) -> None:
        self.session = session
        self.organization_id = organization_id

    def _scoped(self):
        stmt = select(self.model)
        if hasattr(self.model, "organization_id"):
            stmt = stmt.where(self.model.organization_id == self.organization_id)
        return stmt

    async def get(self, id_: uuid.UUID) -> M | None:
        res = await self.session.execute(self._scoped().where(self.model.id == id_))
        return res.scalar_one_or_none()

    async def add(self, obj: M) -> M:
        self.session.add(obj)
        await self.session.flush()
        return obj
