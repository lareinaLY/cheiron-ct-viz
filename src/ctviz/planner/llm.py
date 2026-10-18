"""Thin wrapper around an OpenAI-compatible chat API with strict structured outputs.

This module and `planner/` are the only places that call an LLM. The model only ever
returns a schema instance (a plan draft), never data values (invariant 1).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import openai
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from ctviz.config import Settings
from ctviz.errors import PlannerError, PlannerErrorCode

if TYPE_CHECKING:
    # The openai SDK's own HTTP client package; only needed to inject a test transport.
    import httpx2

logger = logging.getLogger(__name__)

# One retry on transient errors (connection errors, timeouts, 408, 409, 429, 5xx); the
# SDK classifies them and backs off. Other 4xx are not retried.
MAX_RETRIES = 1


class LlmClient(Protocol):
    """What the planner needs from an LLM; tests substitute a fake."""

    @property
    def model(self) -> str:
        """Model name, reported in meta.interpretation."""
        ...

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> T:
        """Return the model's answer parsed as `schema`."""
        ...


class OpenAiLlm:
    """Calls `chat.completions.parse` with `schema` as a strict JSON schema."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None,
        timeout_seconds: float,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=MAX_RETRIES,
            http_client=http_client,
        )

    @property
    def model(self) -> str:
        """Model name, reported in meta.interpretation."""
        return self._model

    async def structured_plan[T: BaseModel](
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[T]
    ) -> T:
        """Return the model's answer parsed as `schema`.

        Raises PlannerError(LLM_UNAVAILABLE) when the provider fails after the retry,
        rejects the request (e.g. a model without structured-output support), refuses,
        or returns no parsable answer.
        """
        try:
            completion = await self._client.chat.completions.parse(
                model=self._model, messages=list(messages), response_format=schema
            )
        except openai.APIStatusError as error:
            raise unavailable(f"HTTP {error.status_code}: {error.message}") from error
        except openai.APIConnectionError as error:
            raise unavailable(f"connection failed: {error}") from error
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as error:
            raise unavailable(f"the answer was cut off: {error}") from error
        except ValidationError as error:
            raise unavailable(f"the answer does not match the schema: {error}") from error
        message = completion.choices[0].message
        if message.refusal:
            raise unavailable(f"the model refused: {message.refusal}")
        if message.parsed is None:
            raise unavailable("the model returned no parsable answer")
        return message.parsed


def unavailable(problem: str) -> PlannerError:
    """A PlannerError for an LLM call that produced no usable answer."""
    logger.warning("LLM request failed: %s", problem)
    return PlannerError(
        PlannerErrorCode.LLM_UNAVAILABLE,
        f"The LLM planner request failed: {problem}",
        ["Retry later.", "Or send structured fields (e.g. `drug` and `dimension`)."],
    )


def build_llm(settings: Settings) -> OpenAiLlm | None:
    """The configured LLM, or None when no API key is set (the planner then uses rules).

    Raises ValueError for a key that cannot be sent in an HTTP header (non-ASCII, usually
    a placeholder left in .env), so a bad key fails at startup instead of on every request.
    The message never includes the key.
    """
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
        return None
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key.isascii() or api_key != api_key.strip():
        raise ValueError(
            "OPENAI_API_KEY contains non-ASCII characters or surrounding whitespace, so it "
            "cannot be a valid key (is it still a placeholder?). Set the real key in .env, "
            "or remove the variable to run without the LLM planner."
        )
    return OpenAiLlm(
        api_key=api_key,
        model=settings.llm_model,
        base_url=settings.openai_base_url or None,
        timeout_seconds=settings.llm_timeout_seconds,
    )
