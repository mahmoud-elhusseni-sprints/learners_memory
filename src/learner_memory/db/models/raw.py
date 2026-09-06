"""Raw evidence ledger and the memory card ledger (Qdrant's system of record)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY, BigInteger, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from learner_memory.db.base import Base, OrgScoped, Timestamps, UUIDPk


class RawDocument(Base, UUIDPk, OrgScoped, Timestamps):
    """L1 index. The bytes live in the single storage bucket at `storage_key`."""

    __tablename__ = "raw_document"

    learner_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="SET NULL"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    extractor_version: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[dict | None] = mapped_column(JSONB)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("organization_id", "source_type", "external_id",
                         name="uq_raw_document_external"),
        UniqueConstraint("organization_id", "content_sha256", name="uq_raw_document_content"),
    )


class MemoryCardRecord(Base, OrgScoped, Timestamps):
    """Ledger mirror of every Qdrant point — same UUID, so drift is repairable."""

    __tablename__ = "memory_card"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), index=True
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("raw_document.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    card_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    salience: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(32), default="extracted", index=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vector_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    extractor_version: Mapped[str] = mapped_column(String(64), index=True)
    prompt_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(96))
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    contributions: Mapped[list] = mapped_column(JSONB, default=list)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class CardContribution(Base):
    """Flattened contributions — answers 'which cards feed skill X' without Qdrant."""

    __tablename__ = "card_contribution"

    card_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory_card.id", ondelete="CASCADE"), primary_key=True
    )
    target: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    level_signal: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    direction: Mapped[str] = mapped_column(String(16), default="supports")

    __table_args__ = (Index("ix_card_contribution_target_key", "target", "key"),)


class JobRun(Base, UUIDPk, Timestamps):
    """Idempotency ledger — a task that already succeeded for a key is a no-op."""

    __tablename__ = "job_run"

    task: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    args: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(JSONB)


class DeadLetter(Base, UUIDPk, Timestamps):
    __tablename__ = "dead_letter"

    task: Mapped[str] = mapped_column(String(128), index=True)
    args: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[dict] = mapped_column(JSONB, default=dict)
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
