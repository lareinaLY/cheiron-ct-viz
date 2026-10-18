"""LLM operations data: per-call records (meta.llm, logs), prices and GET /health/llm."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field


class ResponseMode(StrEnum):
    """How an LLM provider is asked for structured output.

    - json_schema_strict: the provider enforces the JSON schema while decoding.
    - json_object: the provider only guarantees valid JSON; the schema is embedded in
      the system prompt and the answer is validated afterwards.
    """

    JSON_SCHEMA_STRICT = "json_schema_strict"
    JSON_OBJECT = "json_object"


class LlmCallOutcome(StrEnum):
    """What happened to one LLM call.

    ok: the answer was used. retry: an infrastructure failure (timeout, connection error,
    429, 5xx) and the same provider is asked once more. fallback: an infrastructure
    failure after that retry moved the chain to the next provider. validation_retry: the
    answer was rejected by plan validation and the question is asked again. failed: no
    further attempt follows.
    """

    OK = "ok"
    RETRY = "retry"
    FALLBACK = "fallback"
    VALIDATION_RETRY = "validation_retry"
    FAILED = "failed"


class LlmCall(BaseModel):
    """One LLM call, as logged (one JSON line) and listed in meta.llm.calls."""

    request_id: str | None = Field(description="The /query request's X-Request-ID.")
    provider: str
    model_requested: str = Field(description="The model configured in LLM_FALLBACK_CHAIN.")
    model_used: str | None = Field(description="The model the provider reported, if any.")
    response_mode: ResponseMode
    latency_ms: float
    input_tokens: int | None = Field(description="None when the provider reported none.")
    output_tokens: int | None
    estimated_cost_usd: float | None = Field(
        description="From the configured price table (LLM_PRICES); None for a model not in "
        "the table or without token counts — never guessed."
    )
    outcome: LlmCallOutcome
    error_class: str | None = Field(
        description="Exception class of a failed or rejected call, e.g. 'RateLimitError'."
    )


class LlmUsage(BaseModel):
    """meta.llm: every LLM call made for the request, with totals.

    A total is None when any call lacks that value (missing is not zero); with no calls
    (a rules-only request) every total is 0.
    """

    calls: list[LlmCall] = Field(default_factory=list)
    total_latency_ms: float = 0.0
    total_input_tokens: int | None = 0
    total_output_tokens: int | None = 0
    total_estimated_cost_usd: float | None = 0.0
    skipped_providers: list[str] = Field(
        default_factory=list,
        description="Providers the chain skipped without a call because their circuit "
        "breaker was open.",
    )

    @classmethod
    def of(cls, calls: list[LlmCall], skipped_providers: list[str]) -> Self:
        """Usage with totals summed over `calls`."""
        return cls(
            calls=calls,
            total_latency_ms=round(sum(call.latency_ms for call in calls), 1),
            total_input_tokens=sum_if_known([call.input_tokens for call in calls]),
            total_output_tokens=sum_if_known([call.output_tokens for call in calls]),
            total_estimated_cost_usd=sum_if_known([call.estimated_cost_usd for call in calls]),
            skipped_providers=skipped_providers,
        )


def sum_if_known[N: (int, float)](values: list[N | None]) -> N | None:
    """The sum, or None when any value is None."""
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)  # type: ignore[return-value]


class LlmPrice(BaseModel):
    """USD per million tokens for one model (config LLM_PRICES)."""

    model_config = ConfigDict(frozen=True)

    input_usd_per_million: float = Field(ge=0)
    output_usd_per_million: float = Field(ge=0)


class CircuitState(StrEnum):
    """A provider's circuit breaker: closed (calls go through), open (skipped), half_open
    (the open period is over and one probe call may go through)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ProbeStatus(StrEnum):
    """Result of one /health/llm probe: ok, failed, or skipped (circuit open, no call)."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProviderHealth(BaseModel):
    """One provider's /health/llm result."""

    provider: str
    model: str
    status: ProbeStatus
    latency_ms: float | None = Field(description="None when no call was made.")
    response_mode: ResponseMode
    error: str | None
    circuit: CircuitState = Field(description="Circuit state after the probe.")
    consecutive_failures: int


class LlmHealth(BaseModel):
    """GET /health/llm: one probe per configured provider."""

    configured: bool = Field(description="False when no provider has an API key.")
    providers: list[ProviderHealth]


class HealthProbeAnswer(BaseModel):
    """The tiny schema a /health/llm probe asks for."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
