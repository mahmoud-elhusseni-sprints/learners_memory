"""Extraction pipeline tasks."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from learner_memory.core.logging import get_logger
from learner_memory.db.models.raw import CardContribution, MemoryCardRecord, RawDocument
from learner_memory.db.session import unit_of_work
from learner_memory.extractors.base import ExtractionInput
from learner_memory.extractors.registry import get_extractor
from learner_memory.llm.client import TraceContext, get_llm_client
from learner_memory.schemas.memory_card import CardStatus, MemoryCard
from learner_memory.storage.supabase import get_storage
from learner_memory.vector.qdrant import get_card_index
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.base import IdempotentTask, claim, complete, run_async

log = get_logger(__name__)


@celery_app.task(name="extract.extract_document", base=IdempotentTask, bind=True)
def extract_document(self, document_id: str, force: bool = False) -> dict:
    return run_async(_extract_document(uuid.UUID(document_id), force=force))


async def _extract_document(document_id: uuid.UUID, *, force: bool = False) -> dict:
    # `force` re-keys the claim so an explicit reprocess is not short-circuited by
    # the idempotency ledger; extraction itself stays idempotent because card ids
    # are deterministic, so a replay upserts the same cards rather than duplicating.
    key = f"extract:{document_id}" + (f":force:{datetime.now(UTC):%Y%m%d%H%M%S}" if force else "")
    if not await claim("extract.extract_document", key, {"document_id": str(document_id)}):
        log.info("extract.skipped_already_done", document_id=str(document_id))
        return {"skipped": True}

    try:
        async with unit_of_work() as s:
            doc = (
                await s.execute(select(RawDocument).where(RawDocument.id == document_id))
            ).scalar_one()
            doc.status = "extracting"
            snapshot = {
                "organization_id": doc.organization_id,
                "learner_id": doc.learner_id,
                "source_type": doc.source_type,
                "storage_key": doc.storage_key,
                "occurred_at": doc.occurred_at,
                "metadata": dict(doc.metadata_ or {}),
            }

        if snapshot["learner_id"] is None:
            # Cards cannot be attributed yet; leave the document for identity resolution.
            async with unit_of_work() as s:
                (await s.execute(select(RawDocument).where(RawDocument.id == document_id))
                 ).scalar_one().status = "pending_identity"
            await complete(key)
            return {"pending_identity": True}

        raw = await get_storage().get(snapshot["storage_key"])
        extractor = get_extractor(snapshot["source_type"])
        cards = await extractor.run(
            ExtractionInput(
                document_id=document_id,
                organization_id=snapshot["organization_id"],
                learner_id=snapshot["learner_id"],
                occurred_at=snapshot["occurred_at"],
                raw=raw,
                metadata=snapshot["metadata"],
            )
        )

        if cards:
            await _persist_cards(cards, extractor.version)

        async with unit_of_work() as s:
            doc = (await s.execute(select(RawDocument).where(RawDocument.id == document_id))
                   ).scalar_one()
            doc.status = "extracted"
            doc.extractor_version = extractor.version

        await complete(key)

        dimensions = sorted({c.index_key for card in cards for c in card.contributions})
        if dimensions:
            celery_app.send_task(
                "profile.schedule_recompute",
                kwargs={
                    "organization_id": str(snapshot["organization_id"]),
                    "learner_id": str(snapshot["learner_id"]),
                    "dimensions": dimensions,
                },
            )
        return {"cards": len(cards), "dimensions": dimensions}

    except Exception as exc:
        async with unit_of_work() as s:
            doc = (await s.execute(select(RawDocument).where(RawDocument.id == document_id))
                   ).scalar_one_or_none()
            if doc:
                doc.status = "failed"
                doc.error = {"message": str(exc)}
        await complete(key, error=str(exc))
        raise


async def _persist_cards(cards: list[MemoryCard], extractor_version: str) -> None:
    """Ledger first, then Qdrant, then stamp vector_synced_at.

    Outbox discipline: a crash between the two writes leaves the row unstamped and
    `maintenance.reconcile_vectors` repairs it. Postgres always wins.
    """
    async with unit_of_work() as s:
        for card in cards:
            await s.merge(MemoryCardRecord(
                id=card.id,
                organization_id=card.organization_id,
                learner_id=card.learner_id,
                source_document_id=card.source_document_id,
                source_type=str(card.source_type),
                card_type=str(card.card_type),
                title=card.title,
                content=card.content,
                evidence_quote=card.evidence_quote,
                observed_at=card.observed_at,
                confidence=card.confidence,
                salience=card.salience,
                status=CardStatus.EXTRACTED.value,
                extractor_version=extractor_version,
                prompt_version=card.prompt_version,
                model=card.model,
                contributions=[c.model_dump() for c in card.contributions],
                payload=card.payload,
                tags=card.tags,
            ))
            for c in card.contributions:
                await s.merge(CardContribution(
                    card_id=card.id, target=c.target, key=c.key,
                    level_signal=c.level_signal, weight=c.weight, direction=c.direction,
                ))

    llm = get_llm_client()
    trace = TraceContext(
        name="embed.cards",
        trace_id=str(cards[0].source_document_id),
        user_id=str(cards[0].learner_id),
        tags=["embedding"],
    )
    vectors = await llm.embed([c.content for c in cards], trace=trace)
    for card in cards:
        card.status = CardStatus.INDEXED
    await get_card_index().upsert(cards, vectors)

    async with unit_of_work() as s:
        now = datetime.now(UTC)
        for card in cards:
            rec = await s.get(MemoryCardRecord, card.id)
            if rec:
                rec.status = CardStatus.INDEXED.value
                rec.vector_synced_at = now
