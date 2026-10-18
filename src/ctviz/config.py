"""Runtime settings, read from environment variables (prefix CTVIZ_) and `.env`."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Retrieval, cache and LLM settings. Defaults follow docs/api_findings.md §6 and §11.

    The LLM settings also accept the provider's conventional names (OPENAI_API_KEY,
    OPENAI_BASE_URL, LLM_MODEL). Without an API key the planner uses structured fields only.
    """

    model_config = SettingsConfigDict(env_prefix="CTVIZ_", env_file=".env", extra="ignore")

    ctgov_base_url: str = Field(
        default="https://clinicaltrials.gov/api/v2",
        validation_alias=AliasChoices("CTGOV_BASE_URL", "CTVIZ_CTGOV_BASE_URL"),
    )
    max_records: int = Field(default=10_000, gt=0, description="Cap on a full fetch.")
    page_size: int = Field(default=1000, gt=0, description="Clamped to the API maximum of 1000.")
    min_request_interval_seconds: float = Field(
        default=1.2, ge=0, description="Token-bucket pacing; 1 req / 1.2 s was sustainable."
    )
    max_retries: int = Field(default=5, ge=0, description="Retries on 429, 5xx and network errors.")
    backoff_base_seconds: float = Field(default=1.0, ge=0)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    version_ttl_seconds: float = Field(
        default=3600.0, ge=0, description="How long /version's dataTimestamp is reused."
    )
    cache_enabled: bool = True
    cache_dir: Path = Path(".cache")
    cache_ttl_seconds: float = Field(default=6 * 3600.0, ge=0)
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "CTVIZ_OPENAI_API_KEY")
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_BASE_URL", "CTVIZ_OPENAI_BASE_URL"),
        description="OpenAI-compatible endpoint; None uses the SDK default.",
    )
    llm_model: str = Field(
        default="gpt-4.1-mini", validation_alias=AliasChoices("LLM_MODEL", "CTVIZ_LLM_MODEL")
    )
    llm_timeout_seconds: float = Field(default=60.0, gt=0, description="Per LLM request.")
