"""Langfuse tracing is wired through litellm's v3-compatible OTEL callback.

Only the callback registration is asserted; exporting spans needs a Langfuse
server and is not exercised here.
"""
from __future__ import annotations

import pytest

from learner_memory.core.config import Settings
from learner_memory.llm import client as llm_module


@pytest.fixture
def clean_litellm_callbacks(monkeypatch):
    monkeypatch.setattr(llm_module.litellm, "callbacks", [])
    monkeypatch.setattr(llm_module.litellm, "success_callback", [])
    monkeypatch.setattr(llm_module.litellm, "failure_callback", [])
    for var in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(langfuse_enabled=True, **overrides)


def test_registers_the_otel_callback_when_keys_are_set(clean_litellm_callbacks):
    llm_module._configure_litellm(_settings(langfuse_public_key="pk", langfuse_secret_key="sk"))

    assert llm_module.litellm.callbacks == ["langfuse_otel"]
    assert "langfuse" not in llm_module.litellm.success_callback
    assert "langfuse" not in llm_module.litellm.failure_callback


def test_registers_nothing_without_keys(clean_litellm_callbacks):
    llm_module._configure_litellm(_settings(langfuse_public_key=None, langfuse_secret_key=None))

    assert llm_module.litellm.callbacks == []


def test_registers_the_callback_once_per_process(clean_litellm_callbacks):
    settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")

    llm_module._configure_litellm(settings)
    llm_module._configure_litellm(settings)

    assert llm_module.litellm.callbacks == ["langfuse_otel"]
