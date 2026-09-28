"""The LMS learner syncs, one per kind of learner event.

Every learner event arrives on the same webhook and differs only in what its
`api_url` includes, so that is how the webhook picks a sync. What all syncs share
(authentication, idempotency, reading the LMS) lives in the route and the
`lms.sync_learner` task; a sync only says what to read and how to apply it.
Adding one is a new `LmsSync` in `_SYNCS`.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.integrations.lms.mapping import to_personal_info
from learner_memory.integrations.lms.schemas import (
    ContextT,
    LmsLearnerEvent,
    LmsProfileContext,
)
from learner_memory.services.learner_profile_sync import LearnerProfileSync


class LmsSyncKind(StrEnum):
    PROFILE = "profile"


# The event fields that narrow a read to one record; each is also the name of
# the context endpoint's query parameter.
ScopeParam = Literal["journey_id", "form_id"]

# (session, organization_id, external_id, context) -> outcome. Runs inside the
# caller's transaction.
Apply = Callable[[AsyncSession, uuid.UUID, int, ContextT], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class LmsSync(Generic[ContextT]):
    kind: LmsSyncKind
    includes: tuple[str, ...]
    context_model: type[ContextT]
    apply: Apply[ContextT]
    scope_param: ScopeParam | None = None

    def scope_id(self, event: LmsLearnerEvent) -> int | None:
        """The id the event narrows this sync to; None for unscoped syncs."""
        return getattr(event, self.scope_param) if self.scope_param else None

    def scope(self, scope_id: int | None) -> dict[str, int] | None:
        """Query parameters that narrow the LMS read.

        Raises ValueError if this sync is scoped and `scope_id` is missing.
        """
        if self.scope_param is None:
            return None
        if scope_id is None:
            raise ValueError(f"{self.kind} sync needs {self.scope_param}")
        return {self.scope_param: scope_id}


async def _apply_profile(
    session: AsyncSession, organization_id: uuid.UUID, external_id: int,
    context: LmsProfileContext,
) -> str:
    sync = LearnerProfileSync(LearnerRepository(session, organization_id))
    return (await sync.apply(external_id, to_personal_info(context))).value


_SYNCS: tuple[LmsSync, ...] = (
    LmsSync(
        kind=LmsSyncKind.PROFILE,
        includes=("profile",),
        context_model=LmsProfileContext,
        apply=_apply_profile,
    ),
)
_BY_INCLUDES = {frozenset(sync.includes): sync for sync in _SYNCS}
_BY_KIND = {sync.kind: sync for sync in _SYNCS}


def sync_for(includes: frozenset[str]) -> LmsSync | None:
    """The sync for an event's includes; None for data not synced yet."""
    return _BY_INCLUDES.get(includes)


def sync_of(kind: LmsSyncKind | str) -> LmsSync:
    """Raises ValueError for a kind with no sync."""
    return _BY_KIND[LmsSyncKind(kind)]
