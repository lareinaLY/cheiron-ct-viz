"""Runtime settings, read from environment variables (prefix CTVIZ_) and `.env`."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TOP_N_FOR_RANKING = 15
DEFAULT_MAX_NETWORK_NODES = 50
DEFAULT_MIN_EDGE_WEIGHT = 2
DEFAULT_NETWORK_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_NETWORK_REDUCED_EDGE_CITATION_CAP = 2
# Judgment calls anchored on the observed baseline (0 skipped records in every live run,
# docs/api_findings.md §15) and on how sensitive rankings are to small losses.
DEFAULT_DEGRADED_SKIP_RATIO = 0.05
DEFAULT_REFUSE_SKIP_RATIO = 0.5
DEFAULT_MIN_SKIPPED_FOR_DEGRADED = 20


class NetworkLimits(BaseModel):
    """Readability and size limits for network responses, passed from settings to the pipeline."""

    model_config = ConfigDict(frozen=True)

    max_nodes: int = DEFAULT_MAX_NETWORK_NODES
    min_edge_weight: int = DEFAULT_MIN_EDGE_WEIGHT
    max_response_bytes: int = DEFAULT_NETWORK_MAX_RESPONSE_BYTES
    reduced_edge_citation_cap: int = DEFAULT_NETWORK_REDUCED_EDGE_CITATION_CAP


class DataQualityThresholds(BaseModel):
    """When skipped (unreadable) records make a result degraded, or too unreliable to return.

    Skip ratio = structurally skipped records / retrieved records, per cohort. Below
    `min_skipped_for_degraded` skipped records the result is always normal, so a small
    sample with a few bad records is not flagged.
    """

    model_config = ConfigDict(frozen=True)

    degraded_skip_ratio: float = Field(default=DEFAULT_DEGRADED_SKIP_RATIO, gt=0, le=1)
    refuse_skip_ratio: float = Field(default=DEFAULT_REFUSE_SKIP_RATIO, gt=0, le=1)
    min_skipped_for_degraded: int = Field(default=DEFAULT_MIN_SKIPPED_FOR_DEGRADED, ge=0)

    @model_validator(mode="after")
    def refuse_above_degraded(self) -> Self:
        """A refuse ratio below the degraded ratio would skip the degraded band."""
        if self.refuse_skip_ratio < self.degraded_skip_ratio:
            raise ValueError(
                f"REFUSE_SKIP_RATIO ({self.refuse_skip_ratio}) must be >= "
                f"DEGRADED_SKIP_RATIO ({self.degraded_skip_ratio})"
            )
        return self


class Settings(BaseSettings):
    """Retrieval, cache and LLM settings. Defaults follow docs/api_findings.md §6 and §11.

    The LLM settings also accept the providers' conventional names (OPENAI_API_KEY,
    OPENAI_BASE_URL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, LLM_FALLBACK_CHAIN). Providers
    in the chain without a key are skipped; without any key the planner uses structured
    fields only.
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
    deepseek_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DEEPSEEK_API_KEY", "CTVIZ_DEEPSEEK_API_KEY")
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "CTVIZ_DEEPSEEK_BASE_URL"),
    )
    llm_fallback_chain: str = Field(
        default="openai:gpt-4.1-mini",
        validation_alias=AliasChoices("LLM_FALLBACK_CHAIN", "CTVIZ_LLM_FALLBACK_CHAIN"),
        description="Comma-separated provider:model entries, tried in order on "
        "infrastructure failures, e.g. 'openai:gpt-4.1-mini,deepseek:deepseek-chat'.",
    )
    llm_timeout_seconds: float = Field(default=60.0, gt=0, description="Per LLM request.")
    default_top_n_for_ranking: int = Field(
        default=DEFAULT_TOP_N_FOR_RANKING,
        gt=0,
        validation_alias=AliasChoices(
            "DEFAULT_TOP_N_FOR_RANKING", "CTVIZ_DEFAULT_TOP_N_FOR_RANKING"
        ),
        description="top_n used when the question asks for a ranking without a number.",
    )
    max_network_nodes: int = Field(
        default=DEFAULT_MAX_NETWORK_NODES,
        gt=0,
        validation_alias=AliasChoices("MAX_NETWORK_NODES", "CTVIZ_MAX_NETWORK_NODES"),
        description="Networks keep this many highest-degree nodes.",
    )
    min_edge_weight: int = Field(
        default=DEFAULT_MIN_EDGE_WEIGHT,
        gt=0,
        validation_alias=AliasChoices("MIN_EDGE_WEIGHT", "CTVIZ_MIN_EDGE_WEIGHT"),
        description="Network edges with fewer co-occurring trials are omitted.",
    )
    network_max_response_bytes: int = Field(
        default=DEFAULT_NETWORK_MAX_RESPONSE_BYTES,
        gt=0,
        validation_alias=AliasChoices(
            "NETWORK_MAX_RESPONSE_BYTES", "CTVIZ_NETWORK_MAX_RESPONSE_BYTES"
        ),
        description="Above this size, network edge citations are cut (edges are kept).",
    )
    network_reduced_edge_citation_cap: int = Field(
        default=DEFAULT_NETWORK_REDUCED_EDGE_CITATION_CAP,
        gt=0,
        validation_alias=AliasChoices(
            "NETWORK_REDUCED_EDGE_CITATION_CAP", "CTVIZ_NETWORK_REDUCED_EDGE_CITATION_CAP"
        ),
        description="Citations kept per edge when over the size budget; 2 cites both "
        "endpoints in one trial.",
    )

    degraded_skip_ratio: float = Field(
        default=DEFAULT_DEGRADED_SKIP_RATIO,
        gt=0,
        le=1,
        validation_alias=AliasChoices("DEGRADED_SKIP_RATIO", "CTVIZ_DEGRADED_SKIP_RATIO"),
        description="At or above this skipped/retrieved ratio a result is 'degraded'.",
    )
    refuse_skip_ratio: float = Field(
        default=DEFAULT_REFUSE_SKIP_RATIO,
        gt=0,
        le=1,
        validation_alias=AliasChoices("REFUSE_SKIP_RATIO", "CTVIZ_REFUSE_SKIP_RATIO"),
        description="At or above this skipped/retrieved ratio no chart is returned.",
    )
    min_skipped_for_degraded: int = Field(
        default=DEFAULT_MIN_SKIPPED_FOR_DEGRADED,
        ge=0,
        validation_alias=AliasChoices("MIN_SKIPPED_FOR_DEGRADED", "CTVIZ_MIN_SKIPPED_FOR_DEGRADED"),
        description="Fewer skipped records than this never degrade or refuse a result.",
    )
    enum_drift_check: bool = Field(
        default=True,
        description="At startup, compare GET /studies/enums with our enums and log new values.",
    )

    def data_quality_thresholds(self) -> DataQualityThresholds:
        """The skip-ratio thresholds as one value for the pipeline."""
        return DataQualityThresholds(
            degraded_skip_ratio=self.degraded_skip_ratio,
            refuse_skip_ratio=self.refuse_skip_ratio,
            min_skipped_for_degraded=self.min_skipped_for_degraded,
        )

    def network_limits(self) -> NetworkLimits:
        """The network caps as one value for the pipeline."""
        return NetworkLimits(
            max_nodes=self.max_network_nodes,
            min_edge_weight=self.min_edge_weight,
            max_response_bytes=self.network_max_response_bytes,
            reduced_edge_citation_cap=self.network_reduced_edge_citation_cap,
        )
