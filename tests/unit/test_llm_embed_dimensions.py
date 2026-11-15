"""embed() must produce vectors the Qdrant collection can store.

litellm is replaced at its network boundary (`litellm.aembedding`); the
LLMClient code runs for real.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from learner_memory.core.config import Settings
from learner_memory.llm import client as llm_module
from learner_memory.llm.client import EmbeddingDimensionMismatch, LLMClient, TraceContext

COLLECTION_DIM = 4
TRACE = TraceContext(name="test.embed")


class FakeEmbeddingProvider:
    """Answers like an embedding API that honours or ignores `dimensions`."""

    def __init__(self, native_dim: int, honours_dimensions: bool):
        self.native_dim = native_dim
        self.honours_dimensions = honours_dimensions
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        dim = kwargs.get("dimensions") if self.honours_dimensions else self.native_dim
        dim = dim or self.native_dim
        return SimpleNamespace(data=[{"embedding": [0.1] * dim} for _ in kwargs["input"]])


def _client(monkeypatch, provider: FakeEmbeddingProvider) -> LLMClient:
    monkeypatch.setattr(llm_module.litellm, "aembedding", provider)
    return LLMClient(Settings(embedding_dim=COLLECTION_DIM))


async def test_embed_requests_the_collection_dimension(monkeypatch):
    provider = FakeEmbeddingProvider(native_dim=3072, honours_dimensions=True)
    llm = _client(monkeypatch, provider)

    vectors = await llm.embed(["a", "b"], trace=TRACE)

    assert provider.calls[0]["dimensions"] == 4
    assert [len(v) for v in vectors] == [4, 4]


async def test_embed_fails_fast_when_the_model_ignores_the_dimension(monkeypatch):
    provider = FakeEmbeddingProvider(native_dim=3072, honours_dimensions=False)
    llm = _client(monkeypatch, provider)

    with pytest.raises(EmbeddingDimensionMismatch, match="3072"):
        await llm.embed(["a"], trace=TRACE)
