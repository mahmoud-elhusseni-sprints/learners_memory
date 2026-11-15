"""Release documents parked as `pending_identity` once their learner registers.

Ingest parks a document when nobody it refers to is registered yet: a
single-learner document keeps the id under `metadata.unresolved_learner_id`, a
meeting keeps its unregistered roster under `metadata.unresolved_participants`.
This module finds the parked documents whose referenced learner now exists in
the same organization, attaches the identity, and returns them as `received`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Text, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.db.models.learner import Learner
from learner_memory.db.models.raw import RawDocument

PENDING_IDENTITY = "pending_identity"
UNRESOLVED_LEARNER_KEY = "unresolved_learner_id"
UNRESOLVED_PARTICIPANTS_KEY = "unresolved_participants"


def _learner_in_document_org(learner_id_text) -> exists:
    return exists().where(
        Learner.id.cast(Text) == learner_id_text,
        Learner.organization_id == RawDocument.organization_id,
    )


def _resolvable_condition():
    single = and_(
        RawDocument.learner_id.is_(None),
        _learner_in_document_org(RawDocument.metadata_[UNRESOLVED_LEARNER_KEY].astext),
    )
    roster = (
        func.jsonb_each_text(RawDocument.metadata_[UNRESOLVED_PARTICIPANTS_KEY])
        .table_valued("key", "value")
        .render_derived()
    )
    multi = exists(
        select(1)
        .select_from(roster)
        .join(Learner, Learner.id.cast(Text) == roster.c.value)
        .where(Learner.organization_id == RawDocument.organization_id)
    )
    return or_(single, multi)


async def claim_resolvable(session: AsyncSession, batch_size: int) -> list[RawDocument]:
    """Lock up to `batch_size` parked documents that can now be attributed.

    Only resolvable rows are selected, so unresolvable ones never crowd them out
    of the batch. `SKIP LOCKED` keeps overlapping sweeps from claiming one row twice.
    """
    stmt = (
        select(RawDocument)
        .where(RawDocument.status == PENDING_IDENTITY, _resolvable_condition())
        .order_by(RawDocument.received_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def attach_identity(session: AsyncSession, doc: RawDocument) -> None:
    """Move the now-registered identities onto the document and mark it received."""
    metadata = dict(doc.metadata_ or {})
    registered = await _registered_ids(session, doc.organization_id, _referenced_ids(metadata))

    unresolved_owner = metadata.get(UNRESOLVED_LEARNER_KEY)
    if doc.learner_id is None and unresolved_owner in registered:
        doc.learner_id = uuid.UUID(unresolved_owner)
        metadata.pop(UNRESOLVED_LEARNER_KEY)

    pending_roster = metadata.get(UNRESOLVED_PARTICIPANTS_KEY) or {}
    newly = {label: lid for label, lid in pending_roster.items() if lid in registered}
    if newly:
        metadata["participants"] = {**(metadata.get("participants") or {}), **newly}
        remaining = {k: v for k, v in pending_roster.items() if k not in newly}
        metadata[UNRESOLVED_PARTICIPANTS_KEY] = remaining

    doc.metadata_ = metadata
    doc.status = "received"


def _referenced_ids(metadata: dict) -> set[str]:
    ids = set((metadata.get(UNRESOLVED_PARTICIPANTS_KEY) or {}).values())
    if owner := metadata.get(UNRESOLVED_LEARNER_KEY):
        ids.add(owner)
    return ids


async def _registered_ids(
    session: AsyncSession, organization_id: uuid.UUID, ids: set[str]
) -> set[str]:
    if not ids:
        return set()
    rows = await session.execute(
        select(Learner.id.cast(Text)).where(
            Learner.organization_id == organization_id, Learner.id.cast(Text).in_(ids)
        )
    )
    return set(rows.scalars().all())
