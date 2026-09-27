"""Single Settings object (12-factor). Everything else reads from here."""
import uuid
from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr, model_validator
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

    # LMS learner webhooks. One shared secret both ways: the LMS sends it as
    # LC-API-KEY on each webhook, and we send it back when reading learner context.
    # Set all three or none; unset leaves the integration off.
    lms_base_url: AnyHttpUrl | None = None
    lms_api_key: SecretStr | None = None
    lms_organization_id: uuid.UUID | None = None
    lms_timeout_seconds: float = 10.0

    @property
    def tracing_on(self) -> bool:
        return bool(self.langfuse_enabled and self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def lms_enabled(self) -> bool:
        return self.lms_base_url is not None

    @model_validator(mode="after")
    def _lms_all_or_nothing(self) -> "Settings":
        configured = (
            self.lms_base_url is not None,
            bool(self.lms_api_key and self.lms_api_key.get_secret_value()),
            self.lms_organization_id is not None,
        )
        if any(configured) and not all(configured):
            raise ValueError(
                "LMS_BASE_URL, LMS_API_KEY and LMS_ORGANIZATION_ID must be set together"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached factory — the single construction point for configuration."""
    return Settings()  # type: ignore[call-arg]
