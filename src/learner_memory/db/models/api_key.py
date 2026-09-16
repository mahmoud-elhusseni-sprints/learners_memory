"""API keys for remote services.

A key authenticates a *service*, not a person: it carries the organization it was
issued for, so every query made under it is tenant-scoped exactly like a JWT one.
Only the hash is stored — the plaintext exists once, when the key is issued.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from learner_memory.db.base import Base, OrgScoped, Timestamps, UUIDPk


class ApiKey(Base, UUIDPk, OrgScoped, Timestamps):
    __tablename__ = "api_key"

    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
