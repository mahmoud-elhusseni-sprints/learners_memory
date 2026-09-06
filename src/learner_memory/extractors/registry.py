"""Self-registering extractor registry.

Adding a source is a *single new file* under `extractors/sources/`:

    @register
    class PodcastExtractor(BaseExtractor):
        source_type = SourceType.PODCAST
        version = "podcast@1.0"
        payload_model = PodcastPayload
        chunker = TranscriptChunker()

No edits to the registry, the API, the workers, the Qdrant schema or the
synthesizer. `load_extractors()` imports the package at startup and the
decorator does the wiring.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from learner_memory.core.logging import get_logger
from learner_memory.schemas.memory_card import SourceType

if TYPE_CHECKING:
    from learner_memory.extractors.base import BaseExtractor

log = get_logger(__name__)
_REGISTRY: dict[SourceType, type["BaseExtractor"]] = {}


class UnknownSourceType(KeyError):
    pass


def register(cls: type["BaseExtractor"]) -> type["BaseExtractor"]:
    if cls.source_type in _REGISTRY and _REGISTRY[cls.source_type] is not cls:
        raise RuntimeError(f"duplicate extractor for {cls.source_type}")
    _REGISTRY[cls.source_type] = cls
    return cls


def get_extractor(source_type: SourceType | str) -> "BaseExtractor":
    """Factory: source type -> ready-to-run extractor instance."""
    key = SourceType(source_type)
    try:
        return _REGISTRY[key]()
    except KeyError as exc:
        raise UnknownSourceType(f"no extractor registered for '{key}'") from exc


def supported_sources() -> list[str]:
    return sorted(s.value for s in _REGISTRY)


def load_extractors() -> None:
    """Import every module in `extractors.sources` so decorators fire."""
    from learner_memory.extractors import sources

    for mod in pkgutil.iter_modules(sources.__path__):
        importlib.import_module(f"{sources.__name__}.{mod.name}")
    log.info("extractors.loaded", sources=supported_sources())
