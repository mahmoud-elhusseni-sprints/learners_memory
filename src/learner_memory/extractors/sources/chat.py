"""Extractor for timestamped learner interactions with mentors, managers and HR.

The source is single-learner: the ingest envelope identifies the learner. Sender
metadata identifies which messages came from that learner and which came from a
support role. Timing is derived in code before the agent sees the transcript so
response latency and unanswered-message evidence are exact rather than estimated.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from learner_memory.extractors.agents.chat import ChatAgent
from learner_memory.extractors.base import BaseExtractor, ExtractionInput
from learner_memory.extractors.chunking import Chunk, TranscriptChunker
from learner_memory.extractors.registry import register
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.chat import InteractionMessage, InteractionTranscript, json_compact
from learner_memory.schemas.memory_card import MemoryCardDraft, SourceType


class ChatPayload(BaseModel):
    conversation_id: str | None = None
    channel: str | None = None
    message_range: str | None = None
    message_count: int = 0
    participant_roles: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None


def _duration(value: timedelta) -> str:
    seconds = max(0, int(value.total_seconds()))
    units = ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second"))
    parts: list[str] = []
    for size, name in units:
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {name}{'' if amount == 1 else 's'}")
        if len(parts) == 2:
            break
    return " ".join(parts) or "0 seconds"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@register
class ChatExtractor(BaseExtractor):
    source_type = SourceType.CHAT
    version = "chat@1.0"
    prompt_version = "v1"
    payload_model = ChatPayload
    chunker = TranscriptChunker(turns_per_chunk=40, overlap=5)

    def __init__(
        self, llm: LLMClient | None = None, prompts: Any = None, agent: ChatAgent | None = None
    ) -> None:
        super().__init__(llm=llm, prompts=prompts)
        self._agent = agent or ChatAgent(self._llm, self._prompts)
        self._messages: list[InteractionMessage] = []
        self._snapshot_at: datetime | None = None

    def parse(self, data: ExtractionInput) -> str:
        document = json.loads(data.raw.decode("utf-8", errors="replace"))
        self._messages = InteractionTranscript.from_document(document).ordered()
        if not self._messages:
            return ""

        latest = self._messages[-1].sent_at
        self._snapshot_at = max(latest, _aware(data.occurred_at))
        learner_sender_id = data.metadata.get("learner_sender_id")
        return "\n".join(
            self._render_message(index, message, learner_sender_id)
            for index, message in enumerate(self._messages)
        )

    def _is_learner(self, message: InteractionMessage, learner_sender_id: Any) -> bool:
        if learner_sender_id is not None and message.sender.id == str(learner_sender_id):
            return True
        return (message.sender.role or "").strip().lower() == "learner"

    def _next_learner(
        self, index: int, learner_sender_id: Any
    ) -> tuple[int, InteractionMessage] | None:
        for next_index, message in enumerate(self._messages[index + 1 :], start=index + 1):
            if self._is_learner(message, learner_sender_id):
                return next_index, message
        return None

    def _pending_before(
        self, index: int, learner_sender_id: Any
    ) -> list[tuple[int, InteractionMessage]]:
        pending: list[tuple[int, InteractionMessage]] = []
        for prior_index in range(index - 1, -1, -1):
            prior = self._messages[prior_index]
            if self._is_learner(prior, learner_sender_id):
                break
            pending.append((prior_index, prior))
        return list(reversed(pending))

    def _render_message(
        self, index: int, message: InteractionMessage, learner_sender_id: Any
    ) -> str:
        started_at = self._messages[0].sent_at
        previous = self._messages[index - 1] if index else None
        fields = [
            f"message:{index}",
            f"sent_at={message.sent_at.isoformat()}",
            f"since_start={_duration(message.sent_at - started_at)}",
            f"sender_role={message.sender.role or 'unknown'}",
            f"sender_id={message.sender.id or 'unknown'}",
        ]
        if previous:
            fields.append(f"since_previous={_duration(message.sent_at - previous.sent_at)}")

        if self._is_learner(message, learner_sender_id):
            pending = self._pending_before(index, learner_sender_id)
            if pending:
                last_index, last_message = pending[-1]
                roles = sorted({item.sender.label for _, item in pending})
                fields.extend(
                    [
                        f"learner_reply_to={last_index}",
                        f"learner_reply_delay={_duration(message.sent_at - last_message.sent_at)}",
                        f"pending_inbound_count={len(pending)}",
                        f"pending_inbound_roles={','.join(roles)}",
                    ]
                )
        else:
            reply = self._next_learner(index, learner_sender_id)
            if reply:
                reply_index, reply_message = reply
                fields.extend(
                    [
                        f"learner_reply={reply_index}",
                        f"learner_reply_after={_duration(reply_message.sent_at - message.sent_at)}",
                    ]
                )
            else:
                fields.extend(
                    [
                        "learner_reply=none_in_transcript",
                        f"unanswered_for={_duration(self._snapshot_at - message.sent_at)}",
                    ]
                )

        if message.attachments:
            fields.append(f"attachments={json_compact(message.attachments)}")
        if message.metadata:
            fields.append(f"metadata={json_compact(message.metadata)}")
        # TranscriptChunker treats one physical line as one turn. Collapse embedded
        # newlines so a multiline chat message keeps its metadata and stable index.
        text = re.sub(r"\s+", " ", message.text).strip() or "(attachment-only message)"
        return f"[{' | '.join(fields)}] {text}"

    async def _extract_chunk(self, data: ExtractionInput, chunk: Chunk) -> list[MemoryCardDraft]:
        trace = TraceContext(
            name=f"extract.{self.source_type}",
            trace_id=str(data.document_id),
            session_id=str(data.document_id),
            user_id=str(data.learner_id),
            tags=["extraction", str(self.source_type), "langgraph"],
            metadata={
                "organization_id": str(data.organization_id),
                "extractor_version": self.version,
                "prompt_version": self.prompt_version,
                "chunk_anchor": chunk.anchor,
            },
        )
        drafts = await self._agent.run(
            chunk=chunk.text,
            occurred_at=data.occurred_at.isoformat(),
            context=self.build_context(data, chunk),
            trace=trace,
        )
        payload = self._payload(data, chunk)
        for draft in drafts:
            # Code-owned provenance wins over any same-named value proposed by the model.
            draft.payload = (draft.payload or {}) | payload
        return drafts

    def _payload(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        roles = sorted({message.sender.label.lower() for message in self._messages})
        match = re.fullmatch(r"turn:(\d+)-(\d+)", chunk.anchor)
        count = int(match.group(2)) - int(match.group(1)) + 1 if match else len(self._messages)
        return ChatPayload(
            conversation_id=data.metadata.get("conversation_id"),
            channel=data.metadata.get("channel"),
            message_range=chunk.anchor,
            message_count=count,
            participant_roles=roles,
            started_at=self._messages[0].sent_at if self._messages else None,
            ended_at=self._messages[-1].sent_at if self._messages else None,
        ).model_dump(mode="json")

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        return {
            "conversation_id": data.metadata.get("conversation_id"),
            "channel": data.metadata.get("channel"),
            "learner_sender_id": data.metadata.get("learner_sender_id"),
        }
