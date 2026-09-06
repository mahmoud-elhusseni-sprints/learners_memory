"""Learner identity and profile tables."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY, Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric,
    SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from learner_memory.db.base import Base, OrgScoped, Timestamps, UUIDPk


class Learner(Base, OrgScoped, Timestamps):
    """The learner id is *supplied by the caller* at registration and used verbatim
    as the primary key, so ids stay identical across every learn-os service."""

    __tablename__ = "learner"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active")
    program_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    cohort_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class LearnerPersonalData(Base, Timestamps):
    """PII island. `source_of_truth` records, per field, who set the current value
    (human > cv > inferred) — the synthesizer may only write fields it outranks."""

    __tablename__ = "learner_personal_data"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[dict] = mapped_column(JSONB, default=dict)
    contact: Mapped[dict] = mapped_column(JSONB, default=dict)
    education: Mapped[list] = mapped_column(JSONB, default=list)
    experience: Mapped[list] = mapped_column(JSONB, default=list)
    languages: Mapped[list] = mapped_column(JSONB, default=list)
    learning_preferences: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_of_truth: Mapped[dict] = mapped_column(JSONB, default=dict)


class SkillCatalog(Base, UUIDPk, Timestamps):
    """Seeded from docs/skills_taxonomy_framework.md (34 general subskills)."""

    __tablename__ = "skill_catalog"

    kind: Mapped[str] = mapped_column(String(16))              # general | technical
    category: Mapped[str | None] = mapped_column(String(64))
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    level_descriptors: Mapped[dict] = mapped_column(JSONB, default=dict)
    best_evidence: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    taxonomy_version: Mapped[str] = mapped_column(String(16), default="1")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SkillAssessment(Base, UUIDPk, OrgScoped, Timestamps):
    """Append-only. One row per recompute; the previous row is marked superseded."""

    __tablename__ = "skill_assessment"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True),
                                                ForeignKey("skill_catalog.id"))
    level: Mapped[int | None] = mapped_column(SmallInteger)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_card_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), default=list
    )
    rationale: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(32), default="llm_synthesis")
    synthesizer_version: Mapped[str] = mapped_column(String(32))
    taxonomy_version: Mapped[str] = mapped_column(String(16))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class LearnerTechnicalSkill(Base, UUIDPk, Timestamps):
    """Learner-declared technical skills, confirmed (or not) by evidence."""

    __tablename__ = "learner_technical_skill"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(128))
    declared_level: Mapped[int | None] = mapped_column(SmallInteger)
    assessed_level: Mapped[int | None] = mapped_column(SmallInteger)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_card_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), default=list
    )
    last_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (UniqueConstraint("learner_id", "label", name="uq_tech_skill_learner_label"),)


class CareerGoal(Base, UUIDPk, Timestamps):
    __tablename__ = "career_goal"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    target_role: Mapped[str | None] = mapped_column(String(255))
    target_date: Mapped[date | None] = mapped_column(Date)
    motivation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class LearningJourney(Base, UUIDPk, Timestamps):
    __tablename__ = "learning_journey"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), index=True
    )
    career_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("career_goal.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active")
    progress: Mapped[float] = mapped_column(Numeric(5, 4), default=0)
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)


class JourneyStep(Base, UUIDPk, Timestamps):
    __tablename__ = "journey_step"

    journey_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learning_journey.id", ondelete="CASCADE"), index=True
    )
    ord: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="planned")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_card_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), default=list
    )
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class LearnerProfile(Base, OrgScoped, Timestamps):
    """Denormalized read model — the only thing the profile GET endpoint touches."""

    __tablename__ = "learner_profile"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learner.id", ondelete="CASCADE"), primary_key=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_dimensions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
