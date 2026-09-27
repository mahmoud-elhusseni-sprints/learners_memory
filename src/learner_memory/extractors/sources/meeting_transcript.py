"""Meeting transcript extractor — multi-learner.

One diarized transcript (sprint planning, standup, retro, follow-up, technical
discussion, ...) yields memory cards for *several* learners at once, in a single
pass. The flow mirrors the assessment source, with two differences that come from
a meeting having no single owner:

    ingest ── diarized JSON ──> POST /v1/ingest/meeting_transcript
      -> the participant roster {speaker_label: learner_id} is validated and
         filtered to registered learners, then stored on the document; the
         document itself has no owner (learner_id is null)
      -> parse()    JSON  -> "SPEAKER: text" turns, no PII
      -> chunker    text  -> speaker-turn windows (anchor = turn range)
      -> agent      chunk -> MemoryCardDraft[], each tagged with a speaker label
      -> _stamp()   draft -> MemoryCard, resolving speaker_label -> learner_id via
                    the roster and dropping any card that names an unknown speaker

`multi_learner = True` is what tells the pipeline to attribute per card rather
than stamping one owner; everything else is the shared machinery in BaseExtractor.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from learner_memory.extractors.agents.meeting_transcript import MeetingTranscriptAgent
from learner_memory.extractors.base import BaseExtractor, ExtractionInput
from learner_memory.extractors.chunking import Chunk, TranscriptChunker
from learner_memory.extractors.registry import register
from learner_memory.llm.client import LLMClient, TraceContext
from learner_memory.schemas.memory_card import MemoryCardDraft, SourceType


class MeetingTranscriptPayload(BaseModel):
    """Per-card payload: enough to trace a claim back to the meeting and speaker."""

    meeting_id: str | None = None
    meeting_type: str | None = None
    title: str | None = None
    subject_label: str | None = None
    turn_range: str | None = None
    participant_count: int = 0


@register
class MeetingTranscriptExtractor(BaseExtractor):
    source_type = SourceType.MEETING_TRANSCRIPT
    version = "meeting_transcript@1.0"
    prompt_version = "v1"
    payload_model = MeetingTranscriptPayload
    chunker = TranscriptChunker(turns_per_chunk=40, overlap=5)
    multi_learner = True

    def __init__(self, llm: LLMClient | None = None, prompts: Any = None,
                 agent: MeetingTranscriptAgent | None = None) -> None:
        super().__init__(llm=llm, prompts=prompts)
        self._agent = agent or MeetingTranscriptAgent(self._llm, self._prompts)

    # ------------------------------------------------------------------ parse

    def parse(self, data: ExtractionInput) -> str:
        """Diarized JSON (whisper-style segments) or plain text -> "SPEAKER: text".

        Speaker labels are preserved verbatim: they are the key the roster and the
        card attribution match on. No participant names are emitted — learners are
        identified by the roster, and card content must stay free of PII.
        """
        text = data.raw.decode("utf-8", errors="replace")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return text
        segments = doc.get("segments") or doc.get("transcript") or []
        return "\n".join(
            f"{s.get('speaker', 'UNKNOWN')}: {(s.get('text') or '').strip()}"
            for s in segments
            if (s.get("text") or "").strip()
        )

    # ------------------------------------------------------------- agent hook

    async def _extract_chunk(self, data: ExtractionInput, chunk: Chunk) -> list[MemoryCardDraft]:
        """Replaces the base class's single LLM call with the LangGraph agent."""
        trace = TraceContext(
            name=f"extract.{self.source_type}",
            trace_id=str(data.document_id),
            session_id=str(data.document_id),
            user_id=None,  # a meeting has no single learner
            tags=["extraction", str(self.source_type), "langgraph"],
            metadata={
                "organization_id": str(data.organization_id),
                "extractor_version": self.version,
                "prompt_version": self.prompt_version,
                "chunk_anchor": chunk.anchor,
                "participants": len(data.participants),
            },
        )
        drafts = await self._agent.run(
            chunk=chunk.text,
            occurred_at=data.occurred_at.isoformat(),
            context=self.build_context(data, chunk),
            trace=trace,
        )
        for draft in drafts:
            draft.payload = self._payload_for(data, chunk, draft) | (draft.payload or {})
        return drafts

    def _payload_for(self, data: ExtractionInput, chunk: Chunk,
                     draft: MemoryCardDraft) -> dict[str, Any]:
        """Provenance is stamped from the envelope and the chunk, not asked of the
        model. Only `subject_label` comes from the draft, and it is validated
        against the roster in _stamp before it can name a learner."""
        m = data.metadata
        return MeetingTranscriptPayload(
            meeting_id=m.get("meeting_id"),
            meeting_type=m.get("meeting_type"),
            title=m.get("title"),
            subject_label=draft.subject_label,
            turn_range=chunk.anchor,
            participant_count=len(data.participants),
        ).model_dump(mode="json")

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        """The agent is told which speaker labels are learners so it attributes
        cards to them and treats every other speaker as context only."""
        labels = sorted(data.participants)
        m = data.metadata
        return {
            "meeting_type": m.get("meeting_type"),
            "title": m.get("title"),
            "participant_labels": labels,
            "participants": ", ".join(labels),
        }
