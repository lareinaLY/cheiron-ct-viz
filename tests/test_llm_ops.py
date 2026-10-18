"""LLM operations: per-call records (log line and meta.llm), request IDs, the circuit
breaker per provider, same-provider retries and GET /health/llm."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import httpx2
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.errors import LlmInfrastructureError, PlannerError, PlannerErrorCode
from ctviz.planner.circuit_breaker import CircuitBreaker
from ctviz.planner.llm import (
    FallbackChain,
    LlmCallLog,
    OpenAICompatibleProvider,
    estimate_cost,
)
from ctviz.planner.plan import plan
from ctviz.request_context import REQUEST_ID
from ctviz.schemas.llm import CircuitState, LlmCallOutcome, LlmPrice, LlmUsage
from ctviz.schemas.plan import Clarification
from ctviz.schemas.request import QueryRequest
from ctviz.schemas.response import ResponseMode
from tests.factories import load_fixture_studies
from tests.test_llm_fallback import INVALID, VALID, FakeProvider, completion, infra_failure

TODAY = date(2026, 10, 18)
BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
PRICES = {"primary-model": LlmPrice(input_usd_per_million=0.4, output_usd_per_million=1.6)}
MESSAGES: list[Any] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def chain_of(*providers: FakeProvider, clock: FakeClock | None = None) -> FallbackChain:
    return FallbackChain(list(providers), prices=PRICES, clock=clock or FakeClock())


async def run_plan(chain: FallbackChain, log: LlmCallLog | None = None) -> Any:
    request = QueryRequest(query="pembrolizumab by phase")
    return await plan(request, chain, today=TODAY, call_log=log)


def llm_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith('{"event": "llm_call"')
    ]


class TestCircuitBreaker:
    def test_opens_after_three_consecutive_failures(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=60, clock=clock)
        for _ in range(2):
            assert breaker.allow()
            breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        assert not breaker.allow()

    def test_a_success_resets_the_count(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=60, clock=FakeClock())
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.consecutive_failures == 1

    def test_half_open_after_the_open_period_allows_one_probe(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=60, clock=clock)
        for _ in range(3):
            breaker.record_failure()
        clock.now += 59.9
        assert not breaker.allow()
        clock.now += 0.1
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.allow()
        assert not breaker.allow()

    def test_successful_probe_closes(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=60, clock=clock)
        for _ in range(3):
            breaker.record_failure()
        clock.now += 60
        assert breaker.allow()
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.allow()

    def test_failed_probe_reopens_for_another_period(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, open_seconds=60, clock=clock)
        for _ in range(3):
            breaker.record_failure()
        clock.now += 60
        assert breaker.allow()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        clock.now += 59
        assert not breaker.allow()
        clock.now += 1
        assert breaker.allow()


class TestChainWithBreaker:
    async def test_open_provider_is_skipped_without_a_call(self) -> None:
        primary = FakeProvider("primary", *[infra_failure("primary")] * 3)
        secondary = FakeProvider("secondary", *[VALID] * 4)
        chain = chain_of(primary, secondary)
        for _ in range(3):
            await run_plan(chain)
        assert primary.calls == 3
        log = LlmCallLog()
        result = await run_plan(chain, log)
        assert primary.calls == 3
        assert result.provider == "secondary"
        assert result.fallback_reason is not None
        assert "circuit open" in result.fallback_reason
        assert log.skipped_providers == ["primary"]
        assert [call.provider for call in log.calls] == ["secondary"]

    async def test_every_provider_open_is_llm_unavailable_without_calls(self) -> None:
        primary = FakeProvider("primary", *[infra_failure("primary")] * 3)
        chain = chain_of(primary)
        for _ in range(3):
            with pytest.raises(PlannerError):
                await run_plan(chain)
        with pytest.raises(PlannerError) as error:
            await run_plan(chain)
        assert error.value.code == PlannerErrorCode.LLM_UNAVAILABLE
        assert "circuit open" in str(error.value)
        assert primary.calls == 3

    async def test_half_open_probe_success_closes_the_circuit(self) -> None:
        clock = FakeClock()
        primary = FakeProvider("primary", *[infra_failure("primary")] * 3, VALID, VALID)
        chain = chain_of(primary, clock=clock)
        for _ in range(3):
            with pytest.raises(PlannerError):
                await run_plan(chain)
        clock.now += 60
        assert (await run_plan(chain)).provider == "primary"
        assert chain.breakers["primary"].state == CircuitState.CLOSED

    async def test_non_infrastructure_failure_does_not_count(self) -> None:
        rejected = PlannerError(PlannerErrorCode.LLM_UNAVAILABLE, "HTTP 400: bad request")
        primary = FakeProvider("primary", *[rejected] * 3)
        chain = chain_of(primary)
        for _ in range(3):
            with pytest.raises(PlannerError):
                await run_plan(chain)
        assert chain.breakers["primary"].state == CircuitState.CLOSED


class TestCallRecords:
    async def test_validation_retry_then_ok_with_cost_and_request_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="ctviz.planner")
        log = LlmCallLog()
        token = REQUEST_ID.set("req-1")
        try:
            await run_plan(chain_of(FakeProvider("primary", INVALID, VALID)), log)
        finally:
            REQUEST_ID.reset(token)
        outcomes = [call.outcome for call in log.calls]
        assert outcomes == [LlmCallOutcome.VALIDATION_RETRY, LlmCallOutcome.OK]
        first, second = log.calls
        assert first.error_class == "PlanValidationError"
        assert second.error_class is None
        assert second.request_id == "req-1"
        assert second.model_requested == "primary-model"
        assert second.estimated_cost_usd == pytest.approx(100 * 0.4 / 1e6 + 20 * 1.6 / 1e6)
        lines = llm_lines(caplog)
        assert [line["outcome"] for line in lines] == ["validation_retry", "ok"]
        assert lines[1]["request_id"] == "req-1"
        assert set(lines[1]) >= {
            "provider",
            "model_requested",
            "model_used",
            "response_mode",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "estimated_cost_usd",
            "error_class",
        }

    async def test_fallback_record_names_the_error_class(self) -> None:
        log = LlmCallLog()
        primary = FakeProvider("primary", infra_failure("primary"))
        await run_plan(chain_of(primary, FakeProvider("secondary", VALID)), log)
        assert [(c.provider, c.outcome) for c in log.calls] == [
            ("primary", LlmCallOutcome.FALLBACK),
            ("secondary", LlmCallOutcome.OK),
        ]
        assert log.calls[0].error_class == "LlmInfrastructureError"
        assert log.calls[0].input_tokens is None

    def test_unknown_model_cost_is_null(self) -> None:
        assert estimate_cost(PRICES, "mystery-model", 100, 20) is None
        assert estimate_cost(PRICES, "primary-model", None, 20) is None
        assert estimate_cost(PRICES, "primary-model", 1_000_000, 0) == pytest.approx(0.4)

    def test_usage_totals_are_null_when_any_part_is_unknown(self) -> None:
        log = LlmCallLog()
        assert log.usage() == LlmUsage(
            calls=[],
            total_latency_ms=0.0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_estimated_cost_usd=0.0,
            skipped_providers=[],
        )


def make_provider(responses: list[httpx2.Response | Exception]) -> OpenAICompatibleProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def no_sleep(seconds: float) -> None:
        return None

    return OpenAICompatibleProvider(
        name="test",
        base_url="https://llm.test/v1",
        api_key="sk-test",
        model="test-model",
        response_mode=ResponseMode.JSON_OBJECT,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        sleep=no_sleep,
    )


ANSWER = json.dumps({"question": "Which grouping?", "options": ["a", "b"]})


class TestSameProviderRetry:
    async def test_retry_then_success_reports_the_retry(self) -> None:
        provider = make_provider(
            [
                httpx2.Response(503, json={"error": {"message": "busy"}}),
                httpx2.Response(200, json=completion(ANSWER)),
            ]
        )
        reply = await provider.structured_plan(MESSAGES, Clarification)
        assert [c.outcome for c in reply.prior_calls] == [LlmCallOutcome.RETRY]
        assert reply.prior_calls[0].error_class == "InternalServerError"
        assert reply.prior_calls[0].provider == "test"

    async def test_two_failures_raise_with_the_retry_record(self) -> None:
        provider = make_provider(
            [httpx2.Response(429, json={"error": {"message": "slow down"}})] * 2
        )
        with pytest.raises(LlmInfrastructureError) as error:
            await provider.structured_plan(MESSAGES, Clarification)
        assert [c.outcome for c in error.value.prior_calls] == [LlmCallOutcome.RETRY]
        assert error.value.error_class == "RateLimitError"

    async def test_chain_records_retry_then_fallback(self) -> None:
        provider = make_provider([httpx2.Response(500, json={"error": {"message": "x"}})] * 2)
        log = LlmCallLog()
        chain = FallbackChain([provider, FakeProvider("secondary", VALID)], clock=FakeClock())
        await run_plan(chain, log)
        assert [(c.provider, c.outcome) for c in log.calls] == [
            ("test", LlmCallOutcome.RETRY),
            ("test", LlmCallOutcome.FALLBACK),
            ("secondary", LlmCallOutcome.OK),
        ]
        assert log.calls[1].error_class == "InternalServerError"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ctgov_base_url": BASE_URL,
        "min_request_interval_seconds": 0.0,
        "backoff_base_seconds": 0.0,
        "max_retries": 0,
        "cache_enabled": False,
        "enum_drift_check": False,
        "openai_api_key": None,
        "deepseek_api_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        studies = load_fixture_studies("studies_pembrolizumab_page")
        router.get("/studies").mock(
            return_value=httpx.Response(200, json={"studies": studies, "totalCount": len(studies)})
        )
        yield router


class TestQueryMeta:
    def test_llm_request_carries_request_id_and_records(self, mock_api: respx.MockRouter) -> None:
        chain = FallbackChain([FakeProvider("primary", VALID)], prices=PRICES, clock=FakeClock())
        with TestClient(create_app(settings(), llm=chain)) as api:
            response = api.post("/query", json={"query": "pembrolizumab by phase"})
        assert response.status_code == 200
        meta = response.json()["meta"]
        request_id = response.headers["x-request-id"]
        assert meta["request_id"] == request_id
        calls = meta["llm"]["calls"]
        assert len(calls) == 1
        assert calls[0]["request_id"] == request_id
        assert calls[0]["outcome"] == "ok"
        assert meta["llm"]["total_input_tokens"] == 100
        assert meta["llm"]["total_estimated_cost_usd"] == pytest.approx(
            calls[0]["estimated_cost_usd"]
        )

    def test_rules_only_request_has_no_calls(self, mock_api: respx.MockRouter) -> None:
        with TestClient(create_app(settings())) as api:
            response = api.post(
                "/query", json={"query": "q", "drug": "pembrolizumab", "dimension": "phase"}
            )
        meta = response.json()["meta"]
        assert meta["llm"]["calls"] == []
        assert meta["request_id"] == response.headers["x-request-id"]

    def test_error_response_carries_a_request_id(self) -> None:
        with TestClient(create_app(settings())) as api:
            first = api.post("/query", json={"query": "q", "drug": "a\\b", "dimension": "phase"})
            second = api.post("/query", json={"query": "q", "drug": "a\\b", "dimension": "phase"})
        assert first.status_code == 422
        assert first.headers["x-request-id"] != second.headers["x-request-id"]


class TestHealthLlm:
    def test_reports_each_provider_uncached(self) -> None:
        ok = FakeProvider("primary", VALID)
        down = FakeProvider("secondary", infra_failure("secondary"))
        chain = FallbackChain([ok, down], clock=FakeClock())
        with TestClient(create_app(settings(), llm=chain)) as api:
            response = api.get("/health/llm")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["configured"] is True
        first, second = body["providers"]
        assert (first["provider"], first["status"], first["error"]) == ("primary", "ok", None)
        assert first["response_mode"] == "json_schema_strict"
        assert first["latency_ms"] is not None
        assert first["circuit"] == "closed"
        assert (second["status"], second["circuit"]) == ("failed", "closed")
        assert "503" in second["error"]

    def test_open_circuit_is_skipped_without_a_call(self) -> None:
        primary = FakeProvider("primary")
        chain = FallbackChain([primary], clock=FakeClock())
        for _ in range(3):
            chain.breakers["primary"].record_failure()
        with TestClient(create_app(settings(), llm=chain)) as api:
            body = api.get("/health/llm").json()
        assert primary.calls == 0
        assert body["providers"][0]["status"] == "skipped"
        assert body["providers"][0]["circuit"] == "open"
        assert body["providers"][0]["latency_ms"] is None

    def test_without_llm_nothing_is_configured(self) -> None:
        with TestClient(create_app(settings())) as api:
            assert api.get("/health/llm").json() == {"configured": False, "providers": []}
