"""BaseExtractor — Template Method.

The pipeline (parse -> chunk -> prompt -> validate -> stamp) lives here once.
A concrete extractor supplies only what is genuinely source-specific:

    source_type      which SourceType it handles
    version          "<name>@<semver>", recorded on every card it produces
    prompt_version   template id under extractors/prompts/
    payload_model    validates the per-source payload the agent returns
    chunker          a Chunk strategy
    parse()          bytes -> canonical text            (override if not text)
    build_context()  extra prompt variables             (optional)
    postprocess()    source-specific card fixups        (optional)
"""
from __future__ import annotations

import uuid
from abc import ABC
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from learner_memory.core.config import get_settings
from learner_memory.core.logging import get_logger
from learner_memory.core.taxonomy import Taxonomy
from learner_memory.extractors.chunking import Chunk, Chunker, WholeDocumentChunker
from learner_memory.extractors.prompt_loader import PromptLibrary
from learner_memory.llm.client import LLMClient, TraceContext, get_llm_client
from learner_memory.schemas.memory_card import (
    ExtractionResult,
    MemoryCard,
    MemoryCardDraft,
    SourceType,
)

log = get_logger(__name__)


class ExtractionInput(BaseModel):
    document_id: uuid.UUID
    organization_id: uuid.UUID
    # Single-learner sources carry the owner here. Multi-learner sources leave it
    # None and supply `participants` instead — each card is attributed to one of them.
    learner_id: uuid.UUID | None = None
    participants: dict[str, uuid.UUID] = Field(default_factory=dict)
    program_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None
    occurred_at: datetime
    raw: bytes
    metadata: dict[str, Any] = {}


class BaseExtractor(ABC):
    source_type: ClassVar[SourceType]
    version: ClassVar[str]
    prompt_version: ClassVar[str] = "v1"
    payload_model: ClassVar[type[BaseModel] | None] = None
    chunker: ClassVar[Chunker] = WholeDocumentChunker() # ME: Default chunker for single-learner sources, which are usually short enough to fit in one LLM call.
    # A multi-learner source (e.g. a meeting) attributes each card to one of several
    # participants via `subject_label`; a single-learner source stamps `learner_id`.
    multi_learner: ClassVar[bool] = False

    def __init__(self, llm: LLMClient | None = None, prompts: PromptLibrary | None = None) -> None:
        self._llm = llm or get_llm_client()
        self._prompts = prompts or PromptLibrary()
        self._settings = get_settings()

    # ---------------- template method (do not override) ----------------

    async def run(self, data: ExtractionInput) -> list[MemoryCard]:
        text = self.parse(data)
        chunks = self.chunker.split(text, meta=data.metadata)
        cards: list[MemoryCard] = []

        for chunk in chunks:
            drafts = await self._extract_chunk(data, chunk)
            for draft in drafts:
                card = self._stamp(data, chunk, draft)
                if card is not None:
                    cards.append(card)

        cards = self.postprocess(data, cards)
        log.info("extract.done", source=self.source_type, document_id=str(data.document_id),
                 chunks=len(chunks), cards=len(cards))
        return cards

    async def _extract_chunk(self, data: ExtractionInput, chunk: Chunk) -> list[MemoryCardDraft]:
        rendered = self._prompts.render(
            source_type=self.source_type,
            version=self.prompt_version,
            context={
                "chunk": chunk.text,
                "taxonomy": Taxonomy.prompt_block(),
                "occurred_at": data.occurred_at.isoformat(),
                **self.build_context(data, chunk),
            },
        )
        trace = TraceContext(
            name=f"extract.{self.source_type}",
            trace_id=str(data.document_id),
            session_id=str(data.document_id),
            user_id=str(data.learner_id),
            tags=["extraction", str(self.source_type)],
            metadata={
                "organization_id": str(data.organization_id),
                "extractor_version": self.version,
                "prompt_version": self.prompt_version,
                "chunk_anchor": chunk.anchor,
            },
        )
        result = await self._llm.structured(
            [
                {"role": "system", "content": rendered.system},
                {"role": "user", "content": rendered.user}
            ],
            schema=ExtractionResult,
            trace=trace,
        )
        return result.cards

    def _resolve_learner(self, data: ExtractionInput, draft: MemoryCardDraft) -> uuid.UUID | None:
        """Which learner a card belongs to. 
        Identity is decided here, never by the
        model: a multi-learner source's card names only a *speaker label*, which is
        matched against the registered roster. A label that is not in the roster —
        an unregistered participant, a non-learner (mentor, PM), or a hallucinated
        label — resolves to None and the card is dropped, so a card can never be
        attributed to the wrong person.
        """
        if not self.multi_learner:
            return data.learner_id
        return data.participants.get((draft.subject_label or "").strip())

    def _stamp(self, data: ExtractionInput, chunk: Chunk,
               draft: MemoryCardDraft) -> MemoryCard | None:
        """Add identity/provenance and enforce the taxonomy contract."""
        learner_id = self._resolve_learner(data, draft)
        if learner_id is None:
            log.info("extract.unattributed_dropped", source=str(self.source_type),
                     subject=draft.subject_label, anchor=chunk.anchor)
            return None

        kept, dropped = Taxonomy.filter_contributions(draft.contributions)
        if dropped:
            # Never invent a skill: unknown keys become tags instead.
            draft.tags = sorted(set(draft.tags) | {d.key for d in dropped})
            log.info("extract.contribution_dropped", keys=[d.key for d in dropped])
        draft.contributions = kept

        if self.payload_model is not None:
            try:
                draft.payload = self.payload_model.model_validate(draft.payload).model_dump(
                    mode="json"
                )
            except ValidationError as exc:
                log.warning("extract.payload_invalid", error=str(exc), anchor=chunk.anchor)
                return None

        # Multi-learner cards must include the learner in the id key, or two
        # participants with a similarly-titled card in the same chunk would collide.
        # Single-learner ids keep their original shape so existing card ids are stable.
        local_key = (
            f"{chunk.anchor}:{learner_id}:{draft.title}"
            if self.multi_learner
            else f"{chunk.anchor}:{draft.title}"
        )
        return MemoryCard(
            id=MemoryCard.deterministic_id(data.document_id, local_key),
            organization_id=data.organization_id,
            learner_id=learner_id,
            program_id=data.program_id,
            cohort_id=data.cohort_id,
            source_type=self.source_type,
            source_document_id=data.document_id,
            ingested_at=datetime.now(UTC),
            extractor_version=self.version,
            prompt_version=self.prompt_version,
            model=self._settings.llm_model,
            **draft.model_dump(exclude={"observed_at"}),
            observed_at=draft.observed_at or data.occurred_at,
        )

    # ---------------- hooks ----------------

    def parse(self, data: ExtractionInput) -> str:
        """bytes -> canonical text. Override for PDF/DOCX/audio JSON."""
        return data.raw.decode("utf-8", errors="replace")

    def build_context(self, data: ExtractionInput, chunk: Chunk) -> dict[str, Any]:
        return {}

    def postprocess(self, data: ExtractionInput, cards: list[MemoryCard]) -> list[MemoryCard]:
        return cards
