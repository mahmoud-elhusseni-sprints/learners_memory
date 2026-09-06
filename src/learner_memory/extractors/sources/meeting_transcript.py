"""Meeting transcript extractor."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from learner_memory.extractors.base import BaseExtractor, ExtractionInput
from learner_memory.extractors.chunking import Chunk, TranscriptChunker
from learner_memory.extractors.registry import register
from learner_memory.schemas.memory_card import SourceType


class MeetingTranscriptPayload(BaseModel):
    meeting_id: str | None = None
    title: str | None = None
    role_in_meeting: str | None = None
    speaker_label: str | None = None
    participants: list[str] = Field(default_factory=list)
    duration_s: float | None = None


@register
class MeetingTranscriptExtractor(BaseExtractor):
    source_type = SourceType.MEETING_TRANSCRIPT
    version = "meeting_transcript@1.0"
    prompt_version = "v1"
    payload_model = MeetingTranscriptPayload
    chunker = TranscriptChunker(turns_per_chunk=40, overlap=5)

    def parse(self, data: ExtractionInput) -> str:
        """Accepts diarized JSON (whisper-style segments) or plain text."""
        text = data.raw.decode("utf-8", errors="replace")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return text
        segments = doc.get("segments") or doc.get("transcript") or []
        return "\n".join(
            f"{s.get('speaker', 'UNKNOWN')}: {s.get('text', '').strip()}" for s in segments
        )

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        return {"speaker_label": data.metadata.get("speaker_label", "the learner")}
