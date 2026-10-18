"""End-to-end tests of POST /query with the planner: a fake LLM, ClinicalTrials.gov mocked."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.errors import LlmInfrastructureError, PlannerError, PlannerErrorCode
from ctviz.planner.llm import FallbackChain, LlmAnswer
from ctviz.schemas.plan import Dimension, Filters, LlmPlanDraft
from ctviz.schemas.response import ResponseMode, VisualizationResponse
from tests.factories import load_fixture_studies

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}


def make_draft(**fields: Any) -> LlmPlanDraft:
    values: dict[str, Any] = {
        "cohorts": [Filters(drug="pembrolizumab")],
        "dimension": Dimension.PHASE,
        "relation": None,
        "chart_hint": None,
        "top_n": None,
        "ranking_intent": False,
        "match_mode": "intervention_name",
        "unsupported_reason": None,
        "clarification": None,
    }
    values.update(fields)
    return LlmPlanDraft(**values)


class FakeLlm:
    model = "fake-model"

    def __init__(self, result: LlmPlanDraft | PlannerError) -> None:
        self.result = result

    async def structured_plan(self, messages: Sequence[Any], schema: type[Any]) -> Any:
        if isinstance(self.result, PlannerError):
            raise self.result
        return LlmAnswer(
            value=self.result,
            provider="fake",
            model=self.model,
            response_mode=ResponseMode.JSON_SCHEMA_STRICT,
        )


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
        cache_enabled=False,
        openai_api_key=None,
    )


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        router.get("/studies").mock(
            return_value=httpx.Response(
                200,
                json={
                    "studies": load_fixture_studies("studies_pembrolizumab_page"),
                    "totalCount": len(load_fixture_studies("studies_pembrolizumab_page")),
                },
            )
        )
        yield router


def post(llm: FakeLlm | None, body: dict[str, Any]) -> httpx.Response:
    with TestClient(create_app(settings(), llm=llm)) as client:
        return client.post("/query", json=body)


def test_query_alone_is_planned_by_the_llm(mock_api: respx.MockRouter) -> None:
    response = post(FakeLlm(make_draft()), {"query": "pembrolizumab trials by phase"})
    assert response.status_code == 200, response.text
    body = VisualizationResponse.model_validate(response.json())
    interpretation = body.meta.interpretation
    assert interpretation.planned_by == "llm"
    assert interpretation.model == "fake-model"
    assert interpretation.draft == make_draft()
    assert interpretation.plan.cohorts[0].filters.drug == "pembrolizumab"
    assert body.visualization.title == "Trials by phase for pembrolizumab"
    sent = dict(mock_api.routes[1].calls[0].request.url.params)
    assert sent["filter.advanced"] == 'AREA[InterventionName]"pembrolizumab"'


def test_override_is_reported_in_meta(mock_api: respx.MockRouter) -> None:
    body = {"query": "nivolumab by phase", "drug": "pembrolizumab"}
    response = post(FakeLlm(make_draft(cohorts=[Filters(drug="nivolumab")])), body)
    assert response.status_code == 200, response.text
    meta = VisualizationResponse.model_validate(response.json()).meta
    assert meta.interpretation.overrides[0].path == "cohorts[0].filters.drug"
    assert any("nivolumab" in a and "pembrolizumab" in a for a in meta.assumptions)


def test_rules_plan_states_that_the_question_was_not_read(mock_api: respx.MockRouter) -> None:
    body = {"query": "q", "drug": "pembrolizumab", "dimension": "phase"}
    response = post(None, body)
    assert response.status_code == 200, response.text
    meta = VisualizationResponse.model_validate(response.json()).meta
    assert meta.interpretation.planned_by == "rules"
    assert any("not interpreted" in a for a in meta.assumptions)


def test_query_alone_without_llm_is_a_structured_error(mock_api: respx.MockRouter) -> None:
    response = post(None, {"query": "pembrolizumab trials per year"})
    assert response.status_code == 422
    assert response.json()["code"] == "llm_not_configured"
    assert response.json()["suggestions"]
    assert not mock_api.routes[1].called


def test_unsupported_question_is_a_structured_error(mock_api: respx.MockRouter) -> None:
    llm = FakeLlm(make_draft(cohorts=[], dimension=None, unsupported_reason="No outcomes."))
    response = post(llm, {"query": "average response rate of pembrolizumab"})
    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_question"
    assert "No outcomes." in response.json()["message"]
    assert not mock_api.routes[1].called


def test_ranking_default_is_stated_in_meta(mock_api: respx.MockRouter) -> None:
    draft = make_draft(dimension=Dimension.COUNTRY, ranking_intent=True)
    response = post(FakeLlm(draft), {"query": "Which countries have the most trials?"})
    assert response.status_code == 200, response.text
    meta = VisualizationResponse.model_validate(response.json()).meta
    assert meta.interpretation.ranking_top_n == 15
    assert meta.top_n is not None
    omitted = len(meta.top_n.omitted_keys)
    assert omitted > 0
    assert (
        "Question asked for a ranking; showing top 15 (configurable); "
        f"{omitted} buckets omitted" in meta.assumptions
    )


def test_ranking_default_is_configurable(mock_api: respx.MockRouter) -> None:
    draft = make_draft(dimension=Dimension.COUNTRY, ranking_intent=True)
    app = create_app(settings().model_copy(update={"default_top_n_for_ranking": 2}), FakeLlm(draft))
    with TestClient(app) as client:
        response = client.post("/query", json={"query": "Which countries have the most?"})
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["top_n"]["top_n"] == 2


def test_llm_failure_is_a_502(mock_api: respx.MockRouter) -> None:
    error = PlannerError(PlannerErrorCode.LLM_UNAVAILABLE, "LLM request failed: timeout")
    response = post(FakeLlm(error), {"query": "q"})
    assert response.status_code == 502
    assert response.json()["code"] == "llm_unavailable"


def test_every_provider_failing_is_a_502(mock_api: respx.MockRouter) -> None:
    class DownProvider:
        response_mode = ResponseMode.JSON_SCHEMA_STRICT

        def __init__(self, name: str) -> None:
            self.name, self.model = name, f"{name}-model"

        async def structured_plan(self, messages: Sequence[Any], schema: type[Any]) -> Any:
            raise LlmInfrastructureError(self.name, "connection failed")

    chain = FallbackChain([DownProvider("openai"), DownProvider("deepseek")])
    with TestClient(create_app(settings(), llm=chain)) as client:
        response = client.post("/query", json={"query": "q"})
    assert response.status_code == 502
    assert response.json()["code"] == "llm_unavailable"
    assert "openai" in response.json()["message"] and "deepseek" in response.json()["message"]
    assert not mock_api.routes[1].called
