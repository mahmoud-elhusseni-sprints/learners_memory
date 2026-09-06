"""Deterministic object keys. One bucket, one object per uploaded file.

    org/{organization_id}/{source_type}/{YYYY}/{MM}/{DD}/{document_id}{ext}

All metadata lives in `raw_document` — the object is the bytes as received and
nothing else, so a re-upload of the same document overwrites the same key.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import PurePosixPath

_EXT_BY_MIME = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/vtt": ".vtt",
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def extension_for(mime_type: str | None, filename: str | None = None) -> str:
    if filename and (suffix := PurePosixPath(filename).suffix):
        return suffix.lower()
    return _EXT_BY_MIME.get((mime_type or "").split(";")[0].strip(), ".bin")


def object_key(
    *,
    organization_id: uuid.UUID,
    source_type: str,
    document_id: uuid.UUID,
    occurred_at: datetime,
    mime_type: str | None = None,
    filename: str | None = None,
) -> str:
    return (
        f"org/{organization_id}/{source_type}/"
        f"{occurred_at:%Y/%m/%d}/{document_id}{extension_for(mime_type, filename)}"
    )
