"""Maintenance sweeps. Deliberately few — each one earns its place."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from learner_memory.core.logging import get_logger
from learner_memory.db.models.raw import MemoryCardRecord, RawDocument
from learner_memory.db.session import unit_of_work
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, run_async

log = get_logger(__name__)
MAX_DOC_ATTEMPTS = 5


@celery_app.task(name="maintenance.retry_failed_documents", base=IdempotentTask)
def retry_failed_documents(batch_size: int = 100) -> dict:
    return run_async(_retry_failed(batch_size))


async def _retry_failed(batch_size: int) -> dict:
    cutoff = datetime.now(UTC) - timedelta(minutes=15)
    async with unit_of_work() as s:
        docs = (
            await s.execute(
                select(RawDocument.id)
                .where(RawDocument.status == "failed", RawDocument.updated_at < cutoff)
                .limit(batch_size)
            )
        ).scalars().all()

    for doc_id in docs:
        celery_app.send_task("extract.extract_document", kwargs={"document_id": str(doc_id)})
    log.info("maintenance.retry_dispatched", count=len(docs))
    return {"dispatched": len(docs)}


@celery_app.task(name="maintenance.reconcile_vectors", base=IdempotentTask)
def reconcile_vectors(batch_size: int = 500) -> dict:
    return run_async(_reconcile(batch_size))


async def _reconcile(batch_size: int) -> dict:
    """Re-index ledger rows whose Qdrant write never landed or went stale.

    This is the single guard on our only dual write — without it, a crash between
    the Postgres commit and the Qdrant upsert silently drops evidence from every
    future profile recompute.
    """
    from learner_memory.llm.client import TraceContext, get_llm_client
    from learner_memory.schemas.memory_card import MemoryCard
    from learner_memory.vector.qdrant import get_card_index

    async with unit_of_work() as s:
        rows = (
            await s.execute(
                select(MemoryCardRecord)
                .where(
                    or_(
                        MemoryCardRecord.vector_synced_at.is_(None),
                        MemoryCardRecord.updated_at > MemoryCardRecord.vector_synced_at,
                    )
                )
                .limit(batch_size)
            )
        ).scalars().all()
        pending = [_to_card(r) for r in rows]

    if not pending:
        return {"resynced": 0}

    llm = get_llm_client()
    vectors = await llm.embed(
        [c.content for c in pending], trace=TraceContext(name="embed.reconcile", tags=["reconcile"])
    )
    await get_card_index().upsert(pending, vectors)

    async with unit_of_work() as s:
        now = datetime.now(UTC)
        for card in pending:
            rec = await s.get(MemoryCardRecord, card.id)
            if rec:
                rec.vector_synced_at = now
    log.info("maintenance.reconciled", count=len(pending))
    return {"resynced": len(pending)}


def _to_card(rec: MemoryCardRecord):
    from learner_memory.schemas.memory_card import Contribution, MemoryCard

    return MemoryCard(
        id=rec.id,
        organization_id=rec.organization_id,
        learner_id=rec.learner_id,
        source_type=rec.source_type,
        source_document_id=rec.source_document_id,
        card_type=rec.card_type,
        title=rec.title,
        content=rec.content,
        evidence_quote=rec.evidence_quote,
        observed_at=rec.observed_at,
        confidence=rec.confidence,
        salience=rec.salience,
        status=rec.status,
        ingested_at=rec.created_at,
        extractor_version=rec.extractor_version,
        prompt_version=rec.prompt_version,
        model=rec.model,
        contributions=[Contribution(**c) for c in (rec.contributions or [])],
        payload=rec.payload or {},
        tags=list(rec.tags or []),
    )
