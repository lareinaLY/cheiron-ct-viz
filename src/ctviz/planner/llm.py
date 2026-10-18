"""LLM providers behind an ordered fallback chain, all asked for structured output.

This module and `planner/` are the only places that call an LLM. The model only ever
returns a schema instance (a plan draft), never data values (invariant 1).

One implementation, `OpenAICompatibleProvider`, covers every configured provider
(OpenAI, DeepSeek). `FallbackChain` tries them in order and moves on only after an
infrastructure failure (timeout, connection error, 429, 5xx). Any other failure is
final: a rejected request or an answer that breaks the schema would most likely fail
the same way on the next provider.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, SecretStr, ValidationError

from ctviz.config import Settings
from ctviz.errors import LlmInfrastructureError, PlannerError, PlannerErrorCode
from ctviz.schemas.response import ResponseMode

if TYPE_CHECKING:
    # The openai SDK's own HTTP client package; only needed to inject a test transport.
    import httpx2

logger = logging.getLogger(__name__)

# One retry on transient errors (connection errors, timeouts, 408, 409, 429, 5xx); the
# SDK classifies them and backs off. Other 4xx are not retried.
MAX_RETRIES = 1

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


class LlmCallOutcome(StrEnum):
    """What happened to one LLM call, as logged.

    ok: the answer was used. retry: the answer was rejected by validation and the
    question is asked again. fallback: an infrastructure failure moved the chain to the
    next provider. failed: no further attempt follows.
    """

    OK = "ok"
    RETRY = "retry"
    FALLBACK = "fallback"
    FAILED = "failed"


@dataclass(frozen=True)
class LlmReply[T]:
    """One provider's parsed answer plus its token usage (None when not reported)."""

    value: T
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


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
    ) -> None:
        self.name = name
        self.model = model
        self.response_mode = response_mode
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=MAX_RETRIES,
            http_client=http_client,
        )

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmReply[T]:
        """Return the answer parsed as `schema`.

        Raises LlmInfrastructureError when the provider times out, cannot be reached or
        answers 429/5xx after the retry; PlannerError(LLM_UNAVAILABLE) when it rejects
        the request (e.g. an unsupported response_format), refuses, or returns no
        answer matching the schema.
        """
        try:
            if self.response_mode == ResponseMode.JSON_SCHEMA_STRICT:
                return await self._parse_strict(messages, schema)
            return await self._parse_json_object(messages, schema)
        except openai.APIStatusError as error:
            problem = f"HTTP {error.status_code}: {error.message}"
            if error.status_code == 429 or error.status_code >= 500:
                raise infrastructure_failure(self.name, problem) from error
            raise unavailable(problem) from error
        except openai.APIConnectionError as error:
            # Includes APITimeoutError.
            raise infrastructure_failure(self.name, f"connection failed: {error}") from error
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
    failure never switches providers by itself.
    """

    def __init__(self, providers: Sequence[LlmProvider]) -> None:
        if not providers:
            raise ValueError("A fallback chain needs at least one provider.")
        self.providers = list(providers)

    @property
    def model(self) -> str:
        """The primary provider's model."""
        return self.providers[0].model

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> LlmAnswer[T]:
        """Return the first provider's answer that arrives, with how it was obtained.

        Raises PlannerError(LLM_UNAVAILABLE) when every provider failed on
        infrastructure, and re-raises any other provider failure immediately.
        """
        failures: list[str] = []
        for index, provider in enumerate(self.providers):
            started = time.perf_counter()
            try:
                result = await provider.structured_plan(messages, schema)
            except LlmInfrastructureError as error:
                is_last = index == len(self.providers) - 1
                log_llm_call(
                    provider.name,
                    provider.model,
                    elapsed_ms(started),
                    None,
                    None,
                    LlmCallOutcome.FAILED if is_last else LlmCallOutcome.FALLBACK,
                )
                failures.append(f"{provider.name} ({provider.model}): {error.problem}")
                continue
            except PlannerError:
                log_llm_call(
                    provider.name,
                    provider.model,
                    elapsed_ms(started),
                    None,
                    None,
                    LlmCallOutcome.FAILED,
                )
                raise
            return LlmAnswer(
                value=result.value,
                provider=provider.name,
                model=result.model or provider.model,
                response_mode=provider.response_mode,
                fallback_reason="; ".join(failures) or None,
                latency_ms=elapsed_ms(started),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        raise PlannerError(
            PlannerErrorCode.LLM_UNAVAILABLE,
            "Every LLM provider in the fallback chain failed: " + "; ".join(failures),
            ["Retry later.", "Or send structured fields (e.g. `drug` and `dimension`)."],
        )


def elapsed_ms(started: float) -> float:
    """Milliseconds since `started` (a perf_counter value)."""
    return round((time.perf_counter() - started) * 1000, 1)


def log_llm_call(
    provider: str,
    model: str,
    latency_ms: float,
    input_tokens: int | None,
    output_tokens: int | None,
    outcome: LlmCallOutcome,
) -> None:
    """One structured log line per LLM call."""
    logger.info(
        "llm_call provider=%s model=%s latency_ms=%.0f input_tokens=%s output_tokens=%s outcome=%s",
        provider,
        model,
        latency_ms,
        input_tokens,
        output_tokens,
        outcome.value,
    )


def log_llm_answer(answer: LlmAnswer[BaseModel], outcome: LlmCallOutcome) -> None:
    """Log the call that produced `answer`, once the planner knows what became of it."""
    log_llm_call(
        answer.provider,
        answer.model,
        answer.latency_ms,
        answer.input_tokens,
        answer.output_tokens,
        outcome,
    )


def infrastructure_failure(provider: str, problem: str) -> LlmInfrastructureError:
    """An LlmInfrastructureError, logged as a warning."""
    logger.warning("LLM request to %s failed: %s", provider, problem)
    return LlmInfrastructureError(provider, problem)


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
    return FallbackChain(providers)
