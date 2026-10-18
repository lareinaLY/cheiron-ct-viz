"""Tests for the cross-provider fallback chain, with fake providers and a mocked transport."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

import httpx2
import pytest

from ctviz.config import Settings
from ctviz.errors import LlmInfrastructureError, PlannerError, PlannerErrorCode
from ctviz.planner.llm import (
    FallbackChain,
    LlmReply,
    OpenAICompatibleProvider,
    build_llm,
    parse_chain,
)
from ctviz.planner.plan import plan
from ctviz.schemas.plan import Clarification, Dimension, Filters, LlmPlanDraft
from ctviz.schemas.request import QueryRequest
from ctviz.schemas.response import ResponseMode

TODAY = date(2026, 10, 17)
MESSAGES: list[Any] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]


def draft(**fields: Any) -> LlmPlanDraft:
    values: dict[str, Any] = {
        "cohorts": [Filters(drug="pembrolizumab")],
        "dimension": Dimension.PHASE,
        "relation": None,
        "chart_hint": None,
        "top_n": None,
        "ranking_direction": None,
        "match_mode": "intervention_name",
        "unsupported_reason": None,
        "clarification": None,
        "unhandled_terms": [],
    }
    values.update(fields)
    return LlmPlanDraft(**values)


VALID = draft()
INVALID = draft(dimension=None)


def infra_failure(name: str) -> LlmInfrastructureError:
    return LlmInfrastructureError(name, "HTTP 503: overloaded")


class FakeProvider:
    """Answers (or raises) from a queue and counts its calls."""

    def __init__(
        self,
        name: str,
        *results: LlmPlanDraft | PlannerError,
        response_mode: ResponseMode = ResponseMode.JSON_SCHEMA_STRICT,
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.response_mode = response_mode
        self.results = list(results)
        self.calls = 0

    async def structured_plan(self, messages: Sequence[Any], schema: type[Any]) -> Any:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, PlannerError):
            raise result
        return LlmReply(value=result, input_tokens=100, output_tokens=20)


async def run_plan(chain: FallbackChain) -> Any:
    return await plan(QueryRequest(query="pembrolizumab by phase"), chain, today=TODAY)


class TestFallbackChain:
    async def test_primary_ok_never_calls_the_secondary(self) -> None:
        primary, secondary = FakeProvider("primary", VALID), FakeProvider("secondary", VALID)
        result = await run_plan(FallbackChain([primary, secondary]))
        assert (primary.calls, secondary.calls) == (1, 0)
        assert result.provider == "primary"
        assert result.model_used == "primary-model"
        assert result.model == "primary-model"
        assert result.response_mode == ResponseMode.JSON_SCHEMA_STRICT
        assert result.fallback_reason is None

    async def test_primary_infrastructure_failure_falls_back_to_the_secondary(self) -> None:
        primary = FakeProvider("primary", infra_failure("primary"))
        secondary = FakeProvider("secondary", VALID, response_mode=ResponseMode.JSON_OBJECT)
        result = await run_plan(FallbackChain([primary, secondary]))
        assert (primary.calls, secondary.calls) == (1, 1)
        assert result.plan.dimension == Dimension.PHASE
        assert result.provider == "secondary"
        assert result.model_used == "secondary-model"
        assert result.model == "primary-model"
        assert result.response_mode == ResponseMode.JSON_OBJECT
        assert result.fallback_reason is not None
        assert "primary" in result.fallback_reason
        assert "503" in result.fallback_reason

    async def test_validation_failure_does_not_switch_providers(self) -> None:
        primary = FakeProvider("primary", INVALID, VALID)
        secondary = FakeProvider("secondary", VALID)
        result = await run_plan(FallbackChain([primary, secondary]))
        assert (primary.calls, secondary.calls) == (2, 0)
        assert result.provider == "primary"
        assert result.fallback_reason is None
        assert len(result.rejected_attempts) == 1

    async def test_two_validation_failures_stay_on_the_primary(self) -> None:
        primary = FakeProvider("primary", INVALID, INVALID)
        secondary = FakeProvider("secondary", VALID)
        with pytest.raises(PlannerError) as error:
            await run_plan(FallbackChain([primary, secondary]))
        assert error.value.code == PlannerErrorCode.PLANNING_FAILED
        assert secondary.calls == 0

    async def test_non_infrastructure_failure_does_not_switch_providers(self) -> None:
        rejected = PlannerError(PlannerErrorCode.LLM_UNAVAILABLE, "HTTP 400: bad request")
        primary = FakeProvider("primary", rejected)
        secondary = FakeProvider("secondary", VALID)
        with pytest.raises(PlannerError) as error:
            await run_plan(FallbackChain([primary, secondary]))
        assert error.value is rejected
        assert secondary.calls == 0

    async def test_all_providers_failing_is_a_structured_error(self) -> None:
        primary = FakeProvider("primary", infra_failure("primary"))
        secondary = FakeProvider("secondary", infra_failure("secondary"))
        with pytest.raises(PlannerError) as error:
            await run_plan(FallbackChain([primary, secondary]))
        assert error.value.code == PlannerErrorCode.LLM_UNAVAILABLE
        assert "primary" in str(error.value)
        assert "secondary" in str(error.value)
        assert error.value.suggestions

    async def test_one_log_line_per_call_with_outcome(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        primary = FakeProvider("primary", infra_failure("primary"), infra_failure("primary"))
        secondary = FakeProvider("secondary", INVALID, VALID)
        caplog.set_level(logging.INFO, logger="ctviz.planner")
        await run_plan(FallbackChain([primary, secondary]))
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm_call")]
        assert len(lines) == 4
        assert "provider=primary" in lines[0] and "outcome=fallback" in lines[0]
        assert "provider=secondary" in lines[1] and "outcome=retry" in lines[1]
        assert "input_tokens=100" in lines[1] and "output_tokens=20" in lines[1]
        assert "provider=primary" in lines[2] and "outcome=fallback" in lines[2]
        assert "provider=secondary" in lines[3] and "outcome=ok" in lines[3]
        assert all("latency_ms=" in line and "model=" in line for line in lines)


def settings(**fields: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "openai_api_key": None, "deepseek_api_key": None}
    values.update(fields)
    return Settings(**values)


class TestBuildChain:
    def test_default_chain_is_openai_only(self) -> None:
        assert parse_chain(Settings(_env_file=None).llm_fallback_chain) == [  # type: ignore[call-arg]
            ("openai", "gpt-4.1-mini")
        ]

    def test_chain_order_is_kept(self) -> None:
        assert parse_chain(" openai:gpt-4.1-mini , deepseek:deepseek-chat ") == [
            ("openai", "gpt-4.1-mini"),
            ("deepseek", "deepseek-chat"),
        ]

    @pytest.mark.parametrize("chain", ["", "openai", "openai:", "mystery:model"])
    def test_malformed_chain_fails_at_startup(self, chain: str) -> None:
        with pytest.raises(ValueError, match="LLM_FALLBACK_CHAIN"):
            parse_chain(chain)

    def test_provider_without_key_is_skipped_with_a_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="ctviz.planner")
        chain = build_llm(
            settings(
                openai_api_key="sk-test",
                llm_fallback_chain="openai:gpt-4.1-mini,deepseek:deepseek-chat",
            )
        )
        assert isinstance(chain, FallbackChain)
        assert [p.name for p in chain.providers] == ["openai"]
        assert any(
            "deepseek" in r.getMessage() and "DEEPSEEK_API_KEY" in r.getMessage()
            for r in caplog.records
        )

    def test_missing_primary_key_leaves_the_secondary(self) -> None:
        chain = build_llm(
            settings(
                deepseek_api_key="ds-test",
                llm_fallback_chain="openai:gpt-4.1-mini,deepseek:deepseek-chat",
            )
        )
        assert isinstance(chain, FallbackChain)
        assert [(p.name, p.model) for p in chain.providers] == [("deepseek", "deepseek-chat")]
        assert chain.model == "deepseek-chat"

    def test_no_keys_means_no_llm(self) -> None:
        chain = settings(llm_fallback_chain="openai:gpt-4.1-mini,deepseek:deepseek-chat")
        assert build_llm(chain) is None

    def test_response_mode_is_recorded_per_provider(self) -> None:
        chain = build_llm(
            settings(
                openai_api_key="sk-test",
                deepseek_api_key="ds-test",
                llm_fallback_chain="openai:gpt-4.1-mini,deepseek:deepseek-chat",
            )
        )
        assert isinstance(chain, FallbackChain)
        assert [p.response_mode for p in chain.providers] == [
            ResponseMode.JSON_SCHEMA_STRICT,
            ResponseMode.JSON_OBJECT,
        ]

    def test_non_ascii_secondary_key_fails_fast_without_echoing_it(self) -> None:
        bad = settings(
            openai_api_key="sk-test",
            deepseek_api_key="你的key",
            llm_fallback_chain="openai:gpt-4.1-mini,deepseek:deepseek-chat",
        )
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY") as error:
            build_llm(bad)
        assert "你的key" not in str(error.value)


def completion(content: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366},
    }


def make_provider(
    handler: Callable[[httpx2.Request], httpx2.Response],
    response_mode: ResponseMode = ResponseMode.JSON_SCHEMA_STRICT,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="test",
        base_url="https://llm.test/v1",
        api_key="sk-test",
        model="test-model",
        response_mode=response_mode,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


ANSWER = json.dumps({"question": "Which grouping?", "options": ["a", "b"]})


class TestOpenAICompatibleProvider:
    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_rate_limit_and_server_errors_are_infrastructure_failures(
        self, status: int
    ) -> None:
        calls: list[int] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(1)
            return httpx2.Response(status, json={"error": {"message": "unavailable"}})

        with pytest.raises(LlmInfrastructureError):
            await make_provider(handler).structured_plan(MESSAGES, Clarification)
        assert len(calls) == 2

    async def test_connection_error_is_an_infrastructure_failure(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        with pytest.raises(LlmInfrastructureError):
            await make_provider(handler).structured_plan(MESSAGES, Clarification)

    async def test_client_error_is_not_an_infrastructure_failure(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(400, json={"error": {"message": "bad response_format"}})

        with pytest.raises(PlannerError) as error:
            await make_provider(handler).structured_plan(MESSAGES, Clarification)
        assert not isinstance(error.value, LlmInfrastructureError)

    async def test_reply_carries_token_usage(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=completion(ANSWER))

        reply = await make_provider(handler).structured_plan(MESSAGES, Clarification)
        assert (reply.input_tokens, reply.output_tokens) == (321, 45)

    async def test_json_object_mode_embeds_the_schema_in_the_system_prompt(self) -> None:
        sent: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion(ANSWER))

        provider = make_provider(handler, ResponseMode.JSON_OBJECT)
        reply = await provider.structured_plan(MESSAGES, Clarification)
        assert reply.value == Clarification(question="Which grouping?", options=["a", "b"])
        body = sent[0]
        assert body["response_format"] == {"type": "json_object"}
        system = body["messages"][0]
        assert system["role"] == "system"
        assert system["content"].startswith("sys")
        assert json.dumps(Clarification.model_json_schema()) in system["content"]
        assert body["messages"][1:] == MESSAGES[1:]

    async def test_json_object_answer_that_breaks_the_schema_is_not_infrastructure(
        self,
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=completion(json.dumps({"question": "q"})))

        provider = make_provider(handler, ResponseMode.JSON_OBJECT)
        with pytest.raises(PlannerError) as error:
            await provider.structured_plan(MESSAGES, Clarification)
        assert not isinstance(error.value, LlmInfrastructureError)
        assert "schema" in str(error.value)
