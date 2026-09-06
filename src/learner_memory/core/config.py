"""Single Settings object (12-factor). Everything else reads from here."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"
    api_port: int = 8000

    database_url: str
    redis_url: str

    qdrant_url: str
    qdrant_api_key: str | None = None
    qdrant_collection: str = "learner_memory_v1"

    supabase_url: str
    supabase_service_key: str
    storage_bucket: str = "learner-evidence"

    # LLM traffic never talks to a vendor directly — always through the proxy.
    litellm_base_url: str
    litellm_api_key: str
    llm_model: str = "claude-sonnet-5"
    llm_timeout_seconds: int = 120
    llm_max_concurrency: int = 8
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 1024

    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_enabled: bool = True

    jwt_issuer: str | None = None
    jwt_jwks_url: str | None = None
    jwt_audience: str = "learner-memory"
    auth_disabled: bool = False

    taxonomy_version: str = "1"
    profile_debounce_seconds: int = 300
    evidence_window_months: int = 18
    decay_half_life_days: int = 120

    @property
    def tracing_on(self) -> bool:
        return bool(self.langfuse_enabled and self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    """Cached factory — the single construction point for configuration."""
    return Settings()  # type: ignore[call-arg]
