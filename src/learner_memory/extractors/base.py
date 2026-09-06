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

from pydantic import BaseModel, ValidationError

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
    learner_id: uuid.UUID
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
    chunker: ClassVar[Chunker] = WholeDocumentChunker()

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
            [{"role": "system", "content": rendered.system},
             {"role": "user", "content": rendered.user}],
            schema=ExtractionResult,
            trace=trace,
        )
        return result.cards

    def _stamp(self, data: ExtractionInput, chunk: Chunk,
               draft: MemoryCardDraft) -> MemoryCard | None:
        """Add identity/provenance and enforce the taxonomy contract."""
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

        return MemoryCard(
            id=MemoryCard.deterministic_id(data.document_id, f"{chunk.anchor}:{draft.title}"),
            organization_id=data.organization_id,
            learner_id=data.learner_id,
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
