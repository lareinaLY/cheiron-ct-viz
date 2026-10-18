"""LLM providers behind an ordered fallback chain, all asked for structured output.

This module and `planner/` are the only places that call an LLM. The model only ever
returns a schema instance (a plan draft), never data values (invariant 1).

One implementation, `OpenAICompatibleProvider`, covers every configured provider
(OpenAI, DeepSeek); it retries an infrastructure failure (timeout, connection error,
429, 5xx) once itself. `FallbackChain` tries the providers in order and moves on only
after such a failure, skipping a provider whose circuit breaker is open. Any other
failure is final: a rejected request or an answer that breaks the schema would most
likely fail the same way on the next provider.

Every call becomes one `LlmCall` record, logged as one JSON line when its outcome is
known and collected for meta.llm (`LlmCallLog`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, SecretStr, ValidationError

from ctviz.config import Settings
from ctviz.errors import (
    InternalInvariantError,
    LlmInfrastructureError,
    PlannerError,
    PlannerErrorCode,
)
from ctviz.planner.circuit_breaker import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_OPEN_SECONDS,
    CircuitBreaker,
)
from ctviz.request_context import REQUEST_ID
from ctviz.schemas.llm import (
    HealthProbeAnswer,
    LlmCall,
    LlmCallOutcome,
    LlmPrice,
    LlmUsage,
    ProbeStatus,
    ProviderHealth,
    ResponseMode,
)

if TYPE_CHECKING:
    # The openai SDK's own HTTP client package; only needed to inject a test transport.
    import httpx2

logger = logging.getLogger(__name__)

# Attempts per provider call: one retry on an infrastructure failure (timeout,
# connection error, 429, 5xx), made here rather than by the SDK so that each attempt is
# its own call record. Other 4xx are not retried.
MAX_ATTEMPTS_PER_PROVIDER = 2
RETRY_DELAY_SECONDS = 0.5

type Sleep = Callable[[float], Awaitable[None]]

# /health/llm asks each provider for this tiny answer.
PROBE_MESSAGES: list[ChatCompletionMessageParam] = [
    {"role": "system", "content": 'You are a health check. Answer with {"ok": true}.'},
    {"role": "user", "content": "ping"},
]

# DeepSeek answers a strict json_schema response_format with HTTP 400 ("This
# response_format type is unavailable now", probed 2026-10-17), so it gets json_object.
RESPONSE_MODES: dict[str, ResponseMode] = {
    "openai": ResponseMode.JSON_SCHEMA_STRICT,
    "deepseek": ResponseMode.JSON_OBJECT,
}

SCHEMA_INSTRUCTIONS = (
    "\n\n## Answer format\nAnswer with one JSON object and nothing else. It must match "
    "this JSON schema exactly (every property present, null where not stated):\n"
)


@dataclass(frozen=True)
class LlmReply[T]:
    """One provider's parsed answer plus its token usage (None when not reported).

    `prior_calls` are the provider's retry records before this answer, `latency_ms` the
    duration of the attempt that produced it (None: not measured by the provider).
    """

    value: T
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    prior_calls: list[LlmCall] = field(default_factory=list)
    latency_ms: float | None = None


@dataclass(frozen=True)
class LlmAnswer[T]:
    """The chain's answer: the parsed value and which provider produced it, and how."""

    value: T
    provider: str
    model: str
    response_mode: ResponseMode
    fallback_reason: str | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_requested: str | None = None
    estimated_cost_usd: float | None = None
    prior_calls: list[LlmCall] = field(default_factory=list)
    skipped_providers: list[str] = field(default_factory=list)


class LlmProvider(Protocol):
    """One provider in the fallback chain; tests substitute fakes."""

    name: str
    model: str
    response_mode: ResponseMode

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmReply[T]:
        """Return the answer parsed as `schema`.

        Raises LlmInfrastructureError on timeouts, connection errors, 429 and 5xx (after
        the retry), and PlannerError for every other failure.
        """
        ...


class LlmClient(Protocol):
    """What the planner needs from an LLM; tests substitute a fake."""

    @property
    def model(self) -> str:
        """Primary model name, reported in meta.interpretation."""
        ...

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmAnswer[T]:
        """Return the answer parsed as `schema`, with the provider that produced it."""
        ...


class OpenAICompatibleProvider:
    """A chat-completions provider asked for `schema` in its `response_mode`.

    json_schema_strict uses `chat.completions.parse`, so the provider enforces the
    schema. json_object embeds the schema in the system prompt and validates the
    answer here; the planner's plan validation guards the result in both modes.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str | None,
        api_key: str,
        model: str,
        response_mode: ResponseMode = ResponseMode.JSON_SCHEMA_STRICT,
        timeout_seconds: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        retry_delay_seconds: float = RETRY_DELAY_SECONDS,
    ) -> None:
        self.name = name
        self.model = model
        self.response_mode = response_mode
        self._sleep = sleep
        self._retry_delay_seconds = retry_delay_seconds
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmReply[T]:
        """Return the answer parsed as `schema`, retrying an infrastructure failure once.

        Raises LlmInfrastructureError when the provider times out, cannot be reached or
        answers 429/5xx twice; PlannerError(LLM_UNAVAILABLE) when it rejects the request
        (e.g. an unsupported response_format), refuses, or returns no answer matching the
        schema. The failed first attempt is logged and returned in `prior_calls` (on the
        reply or on the raised error).
        """
        retries: list[LlmCall] = []
        for attempt in range(1, MAX_ATTEMPTS_PER_PROVIDER + 1):
            started = time.perf_counter()
            try:
                reply = await self._attempt(messages, schema)
            except LlmInfrastructureError as error:
                error.latency_ms = elapsed_ms(started)
                if attempt == MAX_ATTEMPTS_PER_PROVIDER:
                    error.prior_calls = retries
                    raise
                retries.append(
                    record_call(
                        failed_call(self, error.latency_ms, LlmCallOutcome.RETRY, error.error_class)
                    )
                )
                await self._sleep(self._retry_delay_seconds)
                continue
            return replace(reply, prior_calls=retries, latency_ms=elapsed_ms(started))
        raise InternalInvariantError("the provider retry loop ended without returning or raising")

    async def _attempt[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmReply[T]:
        """One request, with the SDK's failures mapped to our two error kinds."""
        try:
            if self.response_mode == ResponseMode.JSON_SCHEMA_STRICT:
                return await self._parse_strict(messages, schema)
            return await self._parse_json_object(messages, schema)
        except openai.APIStatusError as error:
            problem = f"HTTP {error.status_code}: {error.message}"
            if error.status_code == 429 or error.status_code >= 500:
                raise infrastructure_failure(self.name, problem, error) from error
            raise unavailable(problem) from error
        except openai.APIConnectionError as error:
            # Includes APITimeoutError.
            raise infrastructure_failure(self.name, f"connection failed: {error}", error) from error
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as error:
            raise unavailable(f"the answer was cut off: {error}") from error
        except ValidationError as error:
            raise unavailable(f"the answer does not match the schema: {error}") from error

    async def _parse_strict[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmReply[T]:
        completion = await self._client.chat.completions.parse(
            model=self.model, messages=list(messages), response_format=schema
        )
        message = completion.choices[0].message
        if message.refusal:
            raise unavailable(f"the model refused: {message.refusal}")
        if message.parsed is None:
            raise unavailable("the model returned no parsable answer")
        return reply(message.parsed, completion)

    async def _parse_json_object[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmReply[T]:
        completion = await self._client.chat.completions.create(
            model=self.model,
            messages=with_schema_instructions(messages, schema),
            response_format={"type": "json_object"},
        )
        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise unavailable("the answer was cut off at the length limit")
        if not choice.message.content:
            raise unavailable("the model returned no answer")
        return reply(schema.model_validate_json(choice.message.content), completion)


def reply[T](value: T, completion: ChatCompletion) -> LlmReply[T]:
    """Wrap a parsed answer with the token usage and model the provider reported."""
    usage = completion.usage
    return LlmReply(
        value=value,
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
        model=completion.model or None,
    )


def with_schema_instructions(
    messages: Sequence[ChatCompletionMessageParam], schema: type[BaseModel]
) -> list[ChatCompletionMessageParam]:
    """The messages with the JSON schema appended to the system prompt (json_object mode)."""
    instructions = SCHEMA_INSTRUCTIONS + json.dumps(schema.model_json_schema())
    first, *rest = messages
    if first["role"] != "system" or not isinstance(first.get("content"), str):
        raise ValueError("json_object mode needs a text system prompt as the first message")
    return [{"role": "system", "content": f"{first['content']}{instructions}"}, *rest]


class FallbackChain:
    """Tries providers in order; moves on only after an infrastructure failure.

    A provider whose answer is rejected later by plan validation is asked again through
    the chain from the start (the retry with the validation error), so a validation
    failure never switches providers by itself. Each provider has a circuit breaker
    (`breakers`, in process memory): one open is skipped without a call. `prices` (model
    -> USD per million tokens) gives each call's estimated cost; `clock` drives the
    breakers.
    """

    def __init__(
        self,
        providers: Sequence[LlmProvider],
        *,
        prices: Mapping[str, LlmPrice] | None = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        open_seconds: float = DEFAULT_OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("A fallback chain needs at least one provider.")
        self.providers = list(providers)
        self.prices = dict(prices or {})
        self.breakers = {
            provider.name: CircuitBreaker(
                failure_threshold=failure_threshold, open_seconds=open_seconds, clock=clock
            )
            for provider in self.providers
        }

    @property
    def model(self) -> str:
        """The primary provider's model."""
        return self.providers[0].model

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmAnswer[T]:
        """Return the first provider's answer that arrives, with how it was obtained.

        Raises PlannerError(LLM_UNAVAILABLE) when every provider failed on
        infrastructure or was skipped (circuit open), and re-raises any other provider
        failure immediately. Failed calls are logged here; the answer's own call is
        logged by the planner once it knows whether the answer was used.
        """
        failures: list[str] = []
        calls: list[LlmCall] = []
        skipped: list[str] = []
        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                skipped.append(provider.name)
                failures.append(
                    f"{provider.name} ({provider.model}): circuit open, skipped without a call"
                )
                continue
            started = time.perf_counter()
            try:
                result = await provider.structured_plan(messages, schema)
            except LlmInfrastructureError as error:
                breaker.record_failure()
                is_last = index == len(self.providers) - 1
                outcome = LlmCallOutcome.FAILED if is_last else LlmCallOutcome.FALLBACK
                latency = error.latency_ms if error.latency_ms is not None else elapsed_ms(started)
                calls.extend(error.prior_calls)
                calls.append(
                    record_call(failed_call(provider, latency, outcome, error.error_class))
                )
                failures.append(f"{provider.name} ({provider.model}): {error.problem}")
                continue
            except PlannerError as error:
                # The provider answered, so it is reachable: not a breaker failure.
                breaker.record_success()
                record_call(
                    failed_call(
                        provider, elapsed_ms(started), LlmCallOutcome.FAILED, error_class(error)
                    )
                )
                raise
            breaker.record_success()
            return LlmAnswer(
                value=result.value,
                provider=provider.name,
                model=result.model or provider.model,
                response_mode=provider.response_mode,
                fallback_reason="; ".join(failures) or None,
                latency_ms=result.latency_ms
                if result.latency_ms is not None
                else elapsed_ms(started),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                model_requested=provider.model,
                estimated_cost_usd=estimate_cost(
                    self.prices, provider.model, result.input_tokens, result.output_tokens
                ),
                prior_calls=[*calls, *result.prior_calls],
                skipped_providers=skipped,
            )
        raise PlannerError(
            PlannerErrorCode.LLM_UNAVAILABLE,
            "Every LLM provider in the fallback chain failed: " + "; ".join(failures),
            ["Retry later.", "Or send structured fields (e.g. `drug` and `dimension`)."],
        )

    async def health(self) -> list[ProviderHealth]:
        """Probe every provider once with a tiny schema; never cached.

        A provider whose circuit is open is skipped without a call. A probe counts for
        the breaker like any call, so a half-open probe that succeeds closes it.
        """
        return [await self._probe(provider) for provider in self.providers]

    async def _probe(self, provider: LlmProvider) -> ProviderHealth:
        breaker = self.breakers[provider.name]
        latency: float | None = None
        problem: str | None = None
        failure_class: str | None = None
        if not breaker.allow():
            status = ProbeStatus.SKIPPED
            problem = "circuit open: skipped without a call"
        else:
            started = time.perf_counter()
            try:
                await provider.structured_plan(PROBE_MESSAGES, HealthProbeAnswer)
            except LlmInfrastructureError as error:
                breaker.record_failure()
                status, problem, failure_class = (
                    ProbeStatus.FAILED,
                    error.problem,
                    error.error_class,
                )
            except PlannerError as error:
                breaker.record_success()
                status, problem, failure_class = ProbeStatus.FAILED, str(error), error_class(error)
            else:
                breaker.record_success()
                status = ProbeStatus.OK
            latency = elapsed_ms(started)
            outcome = LlmCallOutcome.OK if status == ProbeStatus.OK else LlmCallOutcome.FAILED
            record_call(failed_call(provider, latency, outcome, failure_class))
        return ProviderHealth(
            provider=provider.name,
            model=provider.model,
            status=status,
            latency_ms=latency,
            response_mode=provider.response_mode,
            error=problem,
            circuit=breaker.state,
            consecutive_failures=breaker.consecutive_failures,
        )


class LlmCallLog:
    """The LLM calls made for one request, for meta.llm; the planner appends to it."""

    def __init__(self) -> None:
        self.calls: list[LlmCall] = []
        self.skipped_providers: list[str] = []

    def add_answer(self, answer: LlmAnswer[BaseModel], answer_call: LlmCall) -> None:
        """Add the calls that led to `answer`, then the answer's own call."""
        self.calls.extend(answer.prior_calls)
        self.calls.append(answer_call)
        for name in answer.skipped_providers:
            if name not in self.skipped_providers:
                self.skipped_providers.append(name)

    def usage(self) -> LlmUsage:
        """meta.llm for these calls."""
        return LlmUsage.of(list(self.calls), list(self.skipped_providers))


def estimate_cost(
    prices: Mapping[str, LlmPrice],
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """USD cost from the price table; None for an unknown model or unknown token counts."""
    price = prices.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    cost = (
        input_tokens * price.input_usd_per_million + output_tokens * price.output_usd_per_million
    ) / 1_000_000
    return round(cost, 8)


def elapsed_ms(started: float) -> float:
    """Milliseconds since `started` (a perf_counter value)."""
    return round((time.perf_counter() - started) * 1000, 1)


def error_class(error: BaseException) -> str:
    """The class name of the exception behind `error` (its cause when it has one)."""
    cause = error.__cause__
    return type(cause if cause is not None else error).__name__


def failed_call(
    provider: LlmProvider, latency_ms: float, outcome: LlmCallOutcome, error_class: str | None
) -> LlmCall:
    """The record of a call that produced no usable answer (no tokens, no cost)."""
    return LlmCall(
        request_id=REQUEST_ID.get(),
        provider=provider.name,
        model_requested=provider.model,
        model_used=None,
        response_mode=provider.response_mode,
        latency_ms=latency_ms,
        input_tokens=None,
        output_tokens=None,
        estimated_cost_usd=None,
        outcome=outcome,
        error_class=error_class,
    )


def answer_call(
    answer: LlmAnswer[BaseModel], outcome: LlmCallOutcome, error_class: str | None = None
) -> LlmCall:
    """The record of the call that produced `answer`, with what became of it."""
    return LlmCall(
        request_id=REQUEST_ID.get(),
        provider=answer.provider,
        model_requested=answer.model_requested or answer.model,
        model_used=answer.model,
        response_mode=answer.response_mode,
        latency_ms=answer.latency_ms,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
        estimated_cost_usd=answer.estimated_cost_usd,
        outcome=outcome,
        error_class=error_class,
    )


def record_call(call: LlmCall) -> LlmCall:
    """Log one JSON line for the call and return it."""
    logger.info("%s", json.dumps({"event": "llm_call", **call.model_dump(mode="json")}))
    return call


def infrastructure_failure(
    provider: str, problem: str, cause: BaseException
) -> LlmInfrastructureError:
    """An LlmInfrastructureError naming the SDK exception, logged as a warning."""
    logger.warning("LLM request to %s failed: %s", provider, problem)
    return LlmInfrastructureError(provider, problem, type(cause).__name__)


def unavailable(problem: str) -> PlannerError:
    """A PlannerError for an LLM call that produced no usable answer."""
    logger.warning("LLM request failed: %s", problem)
    return PlannerError(
        PlannerErrorCode.LLM_UNAVAILABLE,
        f"The LLM planner request failed: {problem}",
        ["Retry later.", "Or send structured fields (e.g. `drug` and `dimension`)."],
    )


def parse_chain(chain: str) -> list[tuple[str, str]]:
    """Parse LLM_FALLBACK_CHAIN ('openai:gpt-4.1-mini,deepseek:deepseek-chat') in order.

    Raises ValueError for an empty chain, an entry without provider and model, or an
    unknown provider.
    """
    entries: list[tuple[str, str]] = []
    for raw_entry in chain.split(","):
        provider, _, model = raw_entry.strip().partition(":")
        provider, model = provider.strip(), model.strip()
        if not provider or not model:
            raise ValueError(
                f"LLM_FALLBACK_CHAIN entry {raw_entry.strip()!r} is not 'provider:model' "
                "(e.g. 'openai:gpt-4.1-mini')."
            )
        if provider not in RESPONSE_MODES:
            raise ValueError(
                f"LLM_FALLBACK_CHAIN names unknown provider {provider!r}; known: "
                f"{', '.join(RESPONSE_MODES)}."
            )
        entries.append((provider, model))
    return entries


def provider_credentials(settings: Settings, provider: str) -> tuple[SecretStr | None, str | None]:
    """The API key and base URL (None: SDK default) configured for `provider`."""
    if provider == "openai":
        return settings.openai_api_key, settings.openai_base_url or None
    if provider == "deepseek":
        return settings.deepseek_api_key, settings.deepseek_base_url
    raise ValueError(f"No credentials are defined for LLM provider {provider!r}.")


def key_variable(provider: str) -> str:
    """The environment variable holding `provider`'s API key."""
    return f"{provider.upper()}_API_KEY"


def build_llm(settings: Settings) -> FallbackChain | None:
    """The configured fallback chain, or None when no provider has a key (rules only).

    A provider whose key is missing is skipped with a log line, never an error, so one
    OpenAI key is enough. Raises ValueError for a malformed chain, or for a key that
    cannot be sent in an HTTP header (non-ASCII, usually a placeholder left in .env), so
    a bad key fails at startup instead of on every request. Messages never include keys.
    """
    providers: list[LlmProvider] = []
    for name, model in parse_chain(settings.llm_fallback_chain):
        secret, base_url = provider_credentials(settings, name)
        if secret is None or not secret.get_secret_value():
            logger.info(
                "LLM provider %s (%s) skipped: %s is not set", name, model, key_variable(name)
            )
            continue
        api_key = secret.get_secret_value()
        if not api_key.isascii() or api_key != api_key.strip():
            raise ValueError(
                f"{key_variable(name)} contains non-ASCII characters or surrounding "
                "whitespace, so it cannot be a valid key (is it still a placeholder?). Set "
                "the real key in .env, or remove the variable to skip this provider."
            )
        providers.append(
            OpenAICompatibleProvider(
                name=name,
                base_url=base_url,
                api_key=api_key,
                model=model,
                response_mode=RESPONSE_MODES[name],
                timeout_seconds=settings.llm_timeout_seconds,
            )
        )
    if not providers:
        return None
    logger.info(
        "LLM fallback chain: %s",
        ", ".join(f"{p.name}:{p.model} ({p.response_mode.value})" for p in providers),
    )
    logger.info(
        "LLM price table as of %s (models: %s); estimated_cost_usd may be out of date",
        settings.llm_price_table_as_of,
        ", ".join(sorted(settings.llm_prices)) or "none",
    )
    return FallbackChain(
        providers,
        prices=settings.llm_prices,
        failure_threshold=settings.llm_circuit_failure_threshold,
        open_seconds=settings.llm_circuit_open_seconds,
    )
