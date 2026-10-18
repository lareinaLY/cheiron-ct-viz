"""Tests for the OpenAI wrapper, with the provider mocked at the HTTP transport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2
import pytest

from ctviz.errors import PlannerError, PlannerErrorCode
from ctviz.planner.llm import OpenAiLlm
from ctviz.schemas.plan import Clarification

MESSAGES = [{"role": "user", "content": "q"}]


def completion(content: str | None, refusal: str | None = None) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }


def make_llm(handler: Callable[[httpx2.Request], httpx2.Response]) -> OpenAiLlm:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAiLlm(
        api_key="sk-test",
        model="test-model",
        base_url="https://llm.test/v1",
        timeout_seconds=5,
        http_client=client,
    )


VALID = json.dumps({"question": "Which grouping?", "options": ["a", "b"]})


async def test_returns_the_parsed_schema_and_sends_a_strict_schema() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=completion(VALID))

    result = await make_llm(handler).structured_plan(MESSAGES, Clarification)
    assert result == Clarification(question="Which grouping?", options=["a", "b"])
    response_format = sent[0]["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert sent[0]["model"] == "test-model"


async def test_transient_error_is_retried_once() -> None:
    responses = [httpx2.Response(500, json={"error": {"message": "boom"}})]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return responses.pop(0) if responses else httpx2.Response(200, json=completion(VALID))

    result = await make_llm(handler).structured_plan(MESSAGES, Clarification)
    assert result.options == ["a", "b"]


async def test_persistent_transient_error_is_llm_unavailable() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(PlannerError) as error:
        await make_llm(handler).structured_plan(MESSAGES, Clarification)
    assert error.value.code == PlannerErrorCode.LLM_UNAVAILABLE
    assert len(calls) == 2


async def test_client_error_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(400, json={"error": {"message": "response_format unsupported"}})

    with pytest.raises(PlannerError) as error:
        await make_llm(handler).structured_plan(MESSAGES, Clarification)
    assert error.value.code == PlannerErrorCode.LLM_UNAVAILABLE
    assert "response_format unsupported" in str(error.value)
    assert len(calls) == 1


async def test_refusal_is_llm_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=completion(None, refusal="I can't help with that."))

    with pytest.raises(PlannerError) as error:
        await make_llm(handler).structured_plan(MESSAGES, Clarification)
    assert error.value.code == PlannerErrorCode.LLM_UNAVAILABLE
    assert "refused" in str(error.value)
