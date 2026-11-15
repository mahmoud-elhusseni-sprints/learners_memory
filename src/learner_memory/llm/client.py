"""LLM access — Adapter over the LiteLLM proxy, traced by Langfuse.

Design notes:
  * Port/Adapter: callers depend on `LLMClient`, never on `litellm` directly, so
    the proxy, the model or the tracer can change without touching extractors.
  * Every call carries trace metadata (learner, document, extractor version) so a
    Langfuse trace can be walked back to the exact raw document that produced it.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, TypeVar

import litellm
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from learner_memory.core.config import Settings, get_settings
from learner_memory.core.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class TraceContext:
    """What Langfuse groups a call under. Flows from the task into every LLM call."""

    name: str
    trace_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None            # learner_id
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_litellm_metadata(self) -> dict[str, Any]:
        return {
            "generation_name": self.name,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "trace_user_id": self.user_id,
            "tags": self.tags,
            **self.metadata,
        }


class SchemaRepairFailed(RuntimeError):
    pass


class EmbeddingDimensionMismatch(RuntimeError):
    """The embedding model returned vectors the Qdrant collection cannot store."""


class LLMClient:
    """Thin async facade over the proxy. One instance per process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._sem = asyncio.Semaphore(self._s.llm_max_concurrency)
        _configure_litellm(self._s)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=30),
        retry=retry_if_exception_type((litellm.RateLimitError, litellm.APIConnectionError,
                                       litellm.Timeout)),
        reraise=True,
    )
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        trace: TraceContext,
        model: str | None = None,
        response_format: dict | None = None,
        temperature: float = 0.0,
    ) -> str:
        async with self._sem:
            resp = await litellm.acompletion(
                model=model or self._s.llm_model,
                messages=messages,
                temperature=temperature,
                response_format=response_format,
                timeout=self._s.llm_timeout_seconds,
                api_base=self._s.litellm_base_url,
                api_key=self._s.litellm_api_key,
                metadata=trace.as_litellm_metadata(),
            )
        return resp.choices[0].message.content or ""

    async def structured(
        self,
        messages: list[dict[str, str]],
        *,
        schema: type[T],
        trace: TraceContext,
        model: str | None = None,
        repair_attempts: int = 1,
    ) -> T:
        """Schema-constrained call with a bounded repair loop.

        The proxy enforces json_schema where the backing model supports it; the
        repair round-trip covers the models that only approximate it.
        """
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(),
                            "strict": True},
        }
        convo = list(messages)
        last_error: Exception | None = None

        for attempt in range(repair_attempts + 1):
            raw = await self.complete(convo, trace=trace, model=model, response_format=fmt)
            try:
                return schema.model_validate_json(raw)
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                log.warning("llm.schema_violation", schema=schema.__name__, attempt=attempt)
                convo = convo + [
                    {"role": "assistant", "content": raw},
                    {"role": "user",
                     "content": f"That did not validate against the schema:\n{exc}\n"
                                f"Return corrected JSON only."},
                ]
        raise SchemaRepairFailed(str(last_error))

    async def embed(self, texts: list[str], *, trace: TraceContext) -> list[list[float]]:
        """Embed `texts` at exactly `EMBEDDING_DIM` dimensions.

        `dimensions` is sent explicitly because models such as
        text-embedding-3-large default to a larger size than the Qdrant
        collection. Raises EmbeddingDimensionMismatch when the provider ignores
        it, so a wrong-sized vector never reaches the upsert.
        """
        expected = self._s.embedding_dim
        async with self._sem:
            resp = await litellm.aembedding(
                model=self._s.embedding_model,
                input=texts,
                dimensions=expected,
                api_base=self._s.litellm_base_url,
                api_key=self._s.litellm_api_key,
                metadata=trace.as_litellm_metadata(),
            )
        vectors = [d["embedding"] for d in resp.data]
        wrong = sorted({len(v) for v in vectors if len(v) != expected})
        if wrong:
            raise EmbeddingDimensionMismatch(
                f"{self._s.embedding_model} returned {wrong}-dimensional vectors but "
                f"EMBEDDING_DIM is {expected}; use a model that supports `dimensions` "
                f"or set EMBEDDING_DIM to match the model and recreate the collection."
            )
        return vectors


# litellm's OpenTelemetry exporter to Langfuse. The classic "langfuse" callback
# imports `langfuse.client`, which the v3 SDK removed; the OTEL one works with
# langfuse v3 and logs both successes and failures from `litellm.callbacks`.
LANGFUSE_CALLBACK = "langfuse_otel"


def _configure_litellm(s: Settings) -> None:
    """Wire Langfuse as a litellm callback once per process, only when keys are set."""
    litellm.drop_params = True
    if s.tracing_on:
        import os

        os.environ.setdefault("LANGFUSE_HOST", s.langfuse_host)
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key or "")
        os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key or "")
        if LANGFUSE_CALLBACK not in litellm.callbacks:
            litellm.callbacks.append(LANGFUSE_CALLBACK)


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
