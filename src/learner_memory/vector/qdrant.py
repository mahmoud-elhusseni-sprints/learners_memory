"""Qdrant adapter: one collection, card UUID as point id."""
from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from learner_memory.core.config import Settings, get_settings
from learner_memory.core.logging import get_logger
from learner_memory.schemas.memory_card import CardStatus, MemoryCard

log = get_logger(__name__)

VECTOR_NAME = "content"
INDEXED_FIELDS = {
    "organization_id": models.PayloadSchemaType.KEYWORD,
    "learner_id": models.PayloadSchemaType.KEYWORD,
    "source_type": models.PayloadSchemaType.KEYWORD,
    "card_type": models.PayloadSchemaType.KEYWORD,
    "status": models.PayloadSchemaType.KEYWORD,
    "contribution_keys": models.PayloadSchemaType.KEYWORD,
    "tags": models.PayloadSchemaType.KEYWORD,
    "observed_at": models.PayloadSchemaType.INTEGER,
}


def to_payload(card: MemoryCard) -> dict[str, Any]:
    return {
        "organization_id": str(card.organization_id),
        "learner_id": str(card.learner_id),
        "program_id": str(card.program_id) if card.program_id else None,
        "source_type": str(card.source_type),
        "source_document_id": str(card.source_document_id),
        "card_type": str(card.card_type),
        "status": str(card.status),
        "observed_at": int(card.observed_at.timestamp()) if card.observed_at else None,
        "confidence": card.confidence,
        "salience": card.salience,
        "contribution_keys": card.contribution_keys(),
        "tags": card.tags,
        "title": card.title,
        "content": card.content,
        "schema_version": card.schema_version,
        "extractor_version": card.extractor_version,
    }


class CardIndex:
    def __init__(self, settings: Settings | None = None,
                 client: AsyncQdrantClient | None = None) -> None:
        self._s = settings or get_settings()
        self._client = client or AsyncQdrantClient(
            url=self._s.qdrant_url, api_key=self._s.qdrant_api_key or None
        )
        self._collection = self._s.qdrant_collection

    async def ensure_collection(self) -> None:
        """Idempotent bootstrap — collections are not Alembic's business."""
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                self._collection,
                vectors_config={
                    VECTOR_NAME: models.VectorParams(
                        size=self._s.embedding_dim, distance=models.Distance.COSINE
                    )
                },
            )
            log.info("qdrant.collection_created", collection=self._collection)
        for field, schema in INDEXED_FIELDS.items():
            try:
                await self._client.create_payload_index(self._collection, field, schema)
            except Exception:  # already indexed
                pass

    async def upsert(self, cards: list[MemoryCard], vectors: list[list[float]]) -> None:
        await self._client.upsert(
            self._collection,
            points=[
                models.PointStruct(id=str(c.id), vector={VECTOR_NAME: v}, payload=to_payload(c))
                for c, v in zip(cards, vectors, strict=True)
            ],
        )

    async def set_status(self, card_id: uuid.UUID, status: CardStatus) -> None:
        await self._client.set_payload(
            self._collection, payload={"status": str(status)}, points=[str(card_id)]
        )

    async def delete(self, card_ids: list[uuid.UUID]) -> None:
        await self._client.delete(
            self._collection,
            points_selector=models.PointIdsList(points=[str(i) for i in card_ids]),
        )

    # ---------------- retrieval ----------------

    def _filter(
        self,
        *,
        organization_id: uuid.UUID,
        learner_id: uuid.UUID | None = None,
        contribution_key: str | None = None,
        source_types: list[str] | None = None,
        card_types: list[str] | None = None,
        since_ts: int | None = None,
        status: CardStatus | None = CardStatus.INDEXED,
    ) -> models.Filter:
        must: list[models.Condition] = [
            models.FieldCondition(key="organization_id",
                                  match=models.MatchValue(value=str(organization_id)))
        ]
        if learner_id:
            must.append(models.FieldCondition(key="learner_id",
                                              match=models.MatchValue(value=str(learner_id))))
        if status:
            must.append(models.FieldCondition(key="status",
                                              match=models.MatchValue(value=str(status))))
        if contribution_key:
            must.append(models.FieldCondition(key="contribution_keys",
                                              match=models.MatchValue(value=contribution_key)))
        if source_types:
            must.append(models.FieldCondition(key="source_type",
                                              match=models.MatchAny(any=source_types)))
        if card_types:
            must.append(models.FieldCondition(key="card_type",
                                              match=models.MatchAny(any=card_types)))
        if since_ts:
            must.append(models.FieldCondition(key="observed_at",
                                              range=models.Range(gte=since_ts)))
        return models.Filter(must=must)

    async def cards_for_dimension(self, *, organization_id: uuid.UUID, learner_id: uuid.UUID,
                                  contribution_key: str, since_ts: int,
                                  limit: int = 200) -> list[dict]:
        """Directed retrieval for the synthesizer: filter-only, no query vector."""
        points, _ = await self._client.scroll(
            self._collection,
            scroll_filter=self._filter(organization_id=organization_id, learner_id=learner_id,
                                       contribution_key=contribution_key, since_ts=since_ts),
            limit=limit,
            with_payload=True,
        )
        return [p.payload for p in points]

    async def search(self, *, organization_id: uuid.UUID, query_vector: list[float],
                     learner_id: uuid.UUID | None = None, top_k: int = 20,
                     **filters) -> list[dict]:
        res = await self._client.query_points(
            self._collection,
            query=query_vector,
            using=VECTOR_NAME,
            query_filter=self._filter(organization_id=organization_id,
                                      learner_id=learner_id, **filters),
            limit=top_k,
            with_payload=True,
        )
        return [{**p.payload, "score": p.score} for p in res.points]


@lru_cache
def get_card_index() -> CardIndex:
    return CardIndex()
