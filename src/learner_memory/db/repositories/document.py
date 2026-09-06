from __future__ import annotations

import uuid

from sqlalchemy import select

from learner_memory.db.models.raw import RawDocument
from learner_memory.db.repositories.base import Repository


class DocumentRepository(Repository[RawDocument]):
    model = RawDocument

    async def by_content_hash(self, content_sha256: str) -> RawDocument | None:
        res = await self.session.execute(
            self._scoped().where(RawDocument.content_sha256 == content_sha256)
        )
        return res.scalar_one_or_none()

    async def by_external_id(self, source_type: str, external_id: str) -> RawDocument | None:
        res = await self.session.execute(
            self._scoped().where(
                RawDocument.source_type == source_type,
                RawDocument.external_id == external_id,
            )
        )
        return res.scalar_one_or_none()

    async def find_duplicate(
        self, *, source_type: str, content_sha256: str, external_id: str | None
    ) -> RawDocument | None:
        """Dedupe on either key — a producer that retries with the same external
        id, or a different producer sending byte-identical content."""
        if external_id and (doc := await self.by_external_id(source_type, external_id)):
            return doc
        return await self.by_content_hash(content_sha256)

    async def mark(self, document_id: uuid.UUID, status: str, **fields) -> None:
        doc = await self.get(document_id)
        if doc:
            doc.status = status
            for k, v in fields.items():
                setattr(doc, k, v)
