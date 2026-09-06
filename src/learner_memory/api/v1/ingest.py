"""Ingestion endpoints. Thin wrappers over IngestService."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from learner_memory.api.deps import AuthDep, LearnerRepoDep, SessionDep
from learner_memory.db.repositories.document import DocumentRepository
from learner_memory.extractors.registry import UnknownSourceType, supported_sources
from learner_memory.schemas.ingest import IngestRequest, IngestResponse
from learner_memory.schemas.memory_card import SourceType
from learner_memory.services.ingest import IngestService
from learner_memory.storage.supabase import get_storage
from learner_memory.workers.celery_app import celery_app

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.get("/sources", response_model=list[str])
async def list_sources() -> list[str]:
    """Whatever is in the extractor registry — no hand-maintained list."""
    return supported_sources()


@router.post("/{source_type}", response_model=IngestResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    source_type: SourceType,
    body: IngestRequest,
    session: SessionDep,
    learners: LearnerRepoDep,
    auth: AuthDep,
) -> IngestResponse:
    auth.require("memory:write")
    if source_type.value not in supported_sources():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"no extractor for '{source_type}'; have {supported_sources()}")

    service = IngestService(
        documents=DocumentRepository(session, auth.organization_id),
        learners=learners,
        storage=get_storage(),
        organization_id=auth.organization_id,
    )
    result = await service.ingest(source_type, body)

    if not result.duplicate and result.status == "received":
        celery_app.send_task(
            "extract.extract_document", kwargs={"document_id": str(result.document_id)}
        )
    return result


@router.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID, session: SessionDep, auth: AuthDep):
    auth.require("evidence:read")
    doc = await DocumentRepository(session, auth.organization_id).get(document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return {
        "id": doc.id, "source_type": doc.source_type, "status": doc.status,
        "learner_id": doc.learner_id, "occurred_at": doc.occurred_at,
        "storage_key": doc.storage_key, "extractor_version": doc.extractor_version,
        "error": doc.error,
    }


@router.post("/documents/{document_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess(document_id: uuid.UUID, session: SessionDep, auth: AuthDep):
    """Re-extract from the archive — the whole point of keeping L1 immutable."""
    auth.require("memory:write")
    repo = DocumentRepository(session, auth.organization_id)
    if await repo.get(document_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    celery_app.send_task("extract.extract_document",
                         kwargs={"document_id": str(document_id), "force": True})
    return {"document_id": document_id, "status": "queued"}
