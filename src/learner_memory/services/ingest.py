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

    async def ingest(
        self, source_type: SourceType, req: IngestRequest, *, multi_learner: bool = False
    ) -> IngestResponse:
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

        owner_id, status, identity_meta = await self._resolve_identity(req, multi_learner)
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
            learner_id=owner_id,
            source_type=source_type.value,
            external_id=req.external_id,
            content_sha256=digest,
            storage_key=key,
            mime_type=req.mime_type,
            size_bytes=len(body),
            occurred_at=req.occurred_at,
            received_at=datetime.now(UTC),
            status=status,
            metadata_={**req.metadata, **identity_meta},
        )
        await self._docs.add(doc)
        log.info("ingest.received", document_id=str(document_id), source=source_type.value,
                 status=status)
        return IngestResponse(document_id=document_id, source_type=source_type, status=doc.status)

    async def _resolve_identity(
        self, req: IngestRequest, multi_learner: bool
    ) -> tuple[uuid.UUID | None, str, dict]:
        """Decide the document's owner, status and identity metadata.

        Single-learner: the owner is the one registered learner. Multi-learner: the
        document has no single owner; instead the participant roster is filtered to
        the learners we actually know, and only that subset is stored. A speaker who
        is not a registered learner (a mentor, or someone not yet onboarded) is left
        out, so extraction never attributes a card to them.

        A document with nothing to attribute is parked as `pending_identity` rather
        than extracted — for a meeting that means none of its participants are
        registered yet.
        """
        if multi_learner:
            roster = await self._resolve_roster(req.metadata.get("participants", {}))
            status = "received" if roster else "pending_identity"
            return None, status, {"participants": {k: str(v) for k, v in roster.items()}}

        learner = await self._learners.get(req.learner_id) if req.learner_id else None
        status = "received" if learner else "pending_identity"
        meta = {} if learner else {"unresolved_learner_id": str(req.learner_id)}
        return (learner.id if learner else None), status, meta

    async def _resolve_roster(self, participants: dict) -> dict[str, uuid.UUID]:
        """Speaker-label -> learner-id, keeping only labels whose learner is
        registered in this organization. Malformed ids are dropped, not fatal —
        one bad entry must not sink the whole meeting."""
        resolved: dict[str, uuid.UUID] = {}
        for label, raw_id in participants.items():
            try:
                learner_id = uuid.UUID(str(raw_id))
            except (ValueError, TypeError, AttributeError):
                log.warning("ingest.participant_id_malformed", label=label)
                continue
            if await self._learners.get(learner_id):
                resolved[str(label)] = learner_id
        return resolved
