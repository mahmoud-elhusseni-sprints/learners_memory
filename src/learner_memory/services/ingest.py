"""Ingestion use case, shared by the HTTP handler and any event consumer.

Keeping this out of the router is what lets a webhook subscriber in
`integrations/` reuse the exact same validation, dedupe and archiving path.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from learner_memory.core.logging import get_logger
from learner_memory.db.models.raw import RawDocument
from learner_memory.db.repositories.document import DocumentRepository
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.schemas.ingest import IngestRequest, IngestResponse
from learner_memory.schemas.memory_card import SourceType
from learner_memory.storage.base import ObjectStorage
from learner_memory.storage.paths import object_key

log = get_logger(__name__)


class IngestService:
    def __init__(
        self,
        documents: DocumentRepository,
        learners: LearnerRepository,
        storage: ObjectStorage,
        organization_id: uuid.UUID,
    ) -> None:
        self._docs = documents
        self._learners = learners
        self._storage = storage
        self._org = organization_id

    async def ingest(self, source_type: SourceType, req: IngestRequest) -> IngestResponse:
        body = (
            req.payload.encode()
            if isinstance(req.payload, str)
            else json.dumps(req.payload, sort_keys=True).encode()
        )
        digest = hashlib.sha256(body).hexdigest()

        if existing := await self._docs.find_duplicate(
            source_type=source_type.value, content_sha256=digest, external_id=req.external_id
        ):
            # Same bytes already archived — return the original, do no work.
            log.info("ingest.duplicate", document_id=str(existing.id))
            return IngestResponse(document_id=existing.id, source_type=source_type,
                                  status=existing.status, duplicate=True)

        learner = await self._learners.resolve(
            learner_id=req.learner_ref.learner_id,
            external_user_id=req.learner_ref.external_user_id,
        )
        document_id = uuid.uuid4()
        key = object_key(
            organization_id=self._org,
            source_type=source_type.value,
            document_id=document_id,
            occurred_at=req.occurred_at,
            mime_type=req.mime_type,
            filename=req.filename,
        )
        # Archive first: the raw evidence must survive even if the rest fails.
        await self._storage.put(key, body, content_type=req.mime_type)

        doc = RawDocument(
            id=document_id,
            organization_id=self._org,
            learner_id=learner.id if learner else None,
            source_type=source_type.value,
            external_id=req.external_id,
            content_sha256=digest,
            storage_key=key,
            mime_type=req.mime_type,
            size_bytes=len(body),
            occurred_at=req.occurred_at,
            received_at=datetime.now(UTC),
            status="received" if learner else "pending_identity",
            metadata_={
                **req.metadata,
                **({"unresolved_learner_ref": req.learner_ref.model_dump(mode="json")}
                   if not learner else {}),
            },
        )
        await self._docs.add(doc)
        log.info("ingest.received", document_id=str(document_id), source=source_type.value,
                 learner_resolved=bool(learner))
        return IngestResponse(document_id=document_id, source_type=source_type, status=doc.status)
