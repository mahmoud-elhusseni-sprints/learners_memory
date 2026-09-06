"""Chunking strategies (Strategy pattern) — reused across extractors."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class Chunk:
    """A unit of evidence handed to one LLM call.

    `anchor` must be stable across re-runs of the same document — it is half of
    the deterministic card id, and therefore what makes re-extraction idempotent.
    """

    anchor: str
    text: str
    meta: dict | None = None


class Chunker(Protocol):
    def split(self, text: str, meta: dict | None = None) -> list[Chunk]: ...


class WholeDocumentChunker:
    """For short, self-contained artifacts (task review, assessment result)."""

    def split(self, text: str, meta: dict | None = None) -> list[Chunk]:
        return [Chunk(anchor="doc:0", text=text, meta=meta)]


class TranscriptChunker:
    """Groups speaker turns into windows, keeping turn ranges as anchors."""

    def __init__(self, turns_per_chunk: int = 40, overlap: int = 5) -> None:
        self.turns_per_chunk = turns_per_chunk
        self.overlap = overlap

    def split(self, text: str, meta: dict | None = None) -> list[Chunk]:
        turns = [t for t in text.splitlines() if t.strip()]
        step = max(1, self.turns_per_chunk - self.overlap)
        chunks: list[Chunk] = []
        for start in range(0, len(turns), step):
            window = turns[start : start + self.turns_per_chunk]
            if not window:
                break
            end = start + len(window) - 1
            chunks.append(Chunk(anchor=f"turn:{start}-{end}", text="\n".join(window), meta=meta))
        return chunks


class SectionChunker:
    """Splits on markdown-ish headings — CVs, reports, long documents."""

    _HEADING = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

    def split(self, text: str, meta: dict | None = None) -> list[Chunk]:
        marks = list(self._HEADING.finditer(text))
        if not marks:
            return WholeDocumentChunker().split(text, meta)
        chunks = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            slug = re.sub(r"[^a-z0-9]+", "-", m.group(2).lower()).strip("-")
            chunks.append(Chunk(anchor=f"section:{slug}", text=text[m.start():end], meta=meta))
        return chunks
