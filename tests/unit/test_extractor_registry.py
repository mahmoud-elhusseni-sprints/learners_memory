import pytest

from learner_memory.extractors.base import BaseExtractor
from learner_memory.extractors.registry import (
    UnknownSourceType, get_extractor, load_extractors, supported_sources,
)
from learner_memory.schemas.memory_card import SourceType


def test_sources_register_themselves_on_load():
    load_extractors()
    assert "task_review" in supported_sources()
    assert "meeting_transcript" in supported_sources()


def test_factory_returns_the_right_extractor():
    load_extractors()
    e = get_extractor(SourceType.TASK_REVIEW)
    assert isinstance(e, BaseExtractor)
    assert e.version.startswith("task_review@")


def test_unregistered_source_fails_loudly():
    load_extractors()
    with pytest.raises(UnknownSourceType):
        get_extractor(SourceType.SELF_REPORT)
