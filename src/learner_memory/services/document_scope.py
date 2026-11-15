"""Program/cohort scope of a raw document, kept in `raw_document.metadata`.

The ingest envelope's `program_id` / `cohort_id` are stored under one reserved
key so they cannot collide with producer metadata (some producers also send a
`cohort_id` inside `metadata`). Extraction and vector reconcile both read it
from here, so every Qdrant point carries the same scope as its document.
"""
from __future__ import annotations

import uuid

SCOPE_KEY = "ingest_scope"


def scope_metadata(program_id: uuid.UUID | None, cohort_id: uuid.UUID | None) -> dict:
    """Metadata fragment to merge into a new document; empty when there is no scope."""
    scope = {
        key: str(value)
        for key, value in (("program_id", program_id), ("cohort_id", cohort_id))
        if value is not None
    }
    return {SCOPE_KEY: scope} if scope else {}


def read_scope(metadata: dict | None) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(program_id, cohort_id) stored on a document; (None, None) when absent."""
    scope = (metadata or {}).get(SCOPE_KEY) or {}
    return _uuid_or_none(scope.get("program_id")), _uuid_or_none(scope.get("cohort_id"))


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    return uuid.UUID(raw) if raw else None
