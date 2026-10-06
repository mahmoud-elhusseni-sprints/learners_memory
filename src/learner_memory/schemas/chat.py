"""Normalized contract for learner interactions with mentors, managers and HR."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class InteractionSender(BaseModel):
    """The stable sender metadata needed to interpret an interaction."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = Field(None, validation_alias=AliasChoices("id", "sender_id", "user_id"))
    role: str | None = Field(None, validation_alias=AliasChoices("role", "type", "sender_type"))

    @classmethod
    def from_value(cls, value: Any) -> InteractionSender:
        if isinstance(value, dict):
            return cls.model_validate(value)
        if isinstance(value, str) and value.strip():
            # A compact sender such as "learner" or "manager" is both its label
            # and its role. Structured senders can provide distinct id and role.
            label = value.strip()
            return cls(id=label, role=label)
        raise ValueError("sender must be a non-empty string or an object")

    @property
    def label(self) -> str:
        return self.role or self.id or "unknown"


class InteractionMessage(BaseModel):
    """One message. Text may be empty when the message only has attachments."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    text: str = Field("", validation_alias=AliasChoices("text", "content", "message", "body"))
    attachments: list[Any] = Field(default_factory=list)
    sender: InteractionSender
    sent_at: datetime = Field(
        validation_alias=AliasChoices("sent_at", "timestamp", "time", "created_at")
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sender", mode="before")
    @classmethod
    def _sender(cls, value: Any) -> InteractionSender:
        return InteractionSender.from_value(value)

    @field_validator("attachments", mode="before")
    @classmethod
    def _attachments(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @field_validator("sent_at")
    @classmethod
    def _timezone(cls, value: datetime) -> datetime:
        # Producers occasionally omit an offset. Treat that form as UTC so timing
        # remains deterministic; timezone-aware values retain their actual instant.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @model_validator(mode="after")
    def _has_content(self) -> InteractionMessage:
        if not self.text.strip() and not self.attachments:
            raise ValueError("a message must contain text or at least one attachment")
        return self


class InteractionTranscript(BaseModel):
    messages: list[InteractionMessage] = Field(default_factory=list)

    @classmethod
    def from_document(cls, document: Any) -> InteractionTranscript:
        """Accept the requested bare list, plus a conventional {messages: [...]} wrapper."""
        if isinstance(document, list):
            return cls(messages=document)
        if isinstance(document, dict) and isinstance(document.get("messages"), list):
            return cls(messages=document["messages"])
        raise ValueError("chat payload must be a JSON list or an object containing 'messages'")

    def ordered(self) -> list[InteractionMessage]:
        """Timestamps, not producer list order, define conversational order."""
        return sorted(self.messages, key=lambda message: message.sent_at)


def json_compact(value: Any) -> str:
    """Stable rendering for attachment and message metadata in the prompt source."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
