"""End-to-end tests of the HTTP API with ClinicalTrials.gov mocked by respx."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.schemas.response import VisualizationResponse
from tests.factories import load_fixture_studies, make_raw_study, set_path

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}


def studies_page(studies: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    total = len(studies) if total is None else total
    return httpx.Response(200, json={"studies": studies, "totalCount": total})


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


@pytest.fixture
def api() -> Iterator[TestClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
        cache_enabled=False,
        enum_drift_check=False,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_phase_query_end_to_end(api: TestClient, mock_api: respx.MockRouter) -> None:
    studies = load_fixture_studies("studies_pembrolizumab_page")
    # A structural failure (phases is a string, not a list) is skipped; an unknown phase
    # inside a list would be kept as Unrecognized instead (tests/test_poison_records.py).
    bad = make_raw_study("NCT09999999")
    set_path(bad, "protocolSection.designModule.phases", "PHASE9")
    route = mock_api.get("/studies").mock(return_value=studies_page([*studies, bad]))

    response = api.post(
        "/query",
        json={"query": "pembrolizumab by phase", "drug": "pembrolizumab", "dimension": "phase"},
    )

    assert response.status_code == 200, response.text
    body = VisualizationResponse.model_validate(response.json())
    sent = dict(route.calls[0].request.url.params)
    assert (
        sent["filter.advanced"]
        == '(AREA[InterventionName]"pembrolizumab" OR AREA[InterventionMeshTerm]"pembrolizumab")'
    )

    visualization, meta = body.visualization, body.meta
    assert visualization.type == "bar_chart"
    assert visualization.title == "Trials by phase for pembrolizumab"
    assert all(datum.citations for datum in visualization.data)
    assert sum(datum.count for datum in visualization.data) == len(studies)

    assert meta.interpretation.question == "pembrolizumab by phase"
    assert meta.interpretation.plan.dimension == "phase"
    assert meta.total_matched == len(studies) + 1
    assert meta.records_retrieved == len(studies) + 1
    assert meta.cohorts[0].records_skipped == 1
    assert meta.cohorts[0].reconciled is True
    assert any("NCT09999999" in w and "PHASE9" in w for w in meta.warnings)
    assert meta.data_timestamp is not None
    assert set(meta.timing_ms) == {
        "validate",
        "retrieve",
        "normalize",
        "aggregate",
        "spec",
        "total",
    }


@pytest.mark.parametrize(
    "version", [{"apiVersion": "2.0.5"}, {"apiVersion": "2.0.5", "dataTimestamp": "n/a"}]
)
def test_missing_data_timestamp_does_not_fail_the_request(
    api: TestClient, mock_api: respx.MockRouter, version: dict[str, Any]
) -> None:
    mock_api.get("/version").mock(return_value=httpx.Response(200, json=version))
    studies = load_fixture_studies("studies_pembrolizumab_page")
    mock_api.get("/studies").mock(return_value=studies_page(studies))
    response = api.post(
        "/query", json={"query": "q", "drug": "pembrolizumab", "dimension": "phase"}
    )
    assert response.status_code == 200, response.text
    meta = VisualizationResponse.model_validate(response.json()).meta
    assert meta.data_timestamp is None
    assert sum(
        d.count for d in VisualizationResponse.model_validate(response.json()).visualization.data
    ) == len(studies)
    assert any("dataTimestamp" in w for w in meta.warnings)


@pytest.mark.parametrize(
    ("version_response", "cause"),
    [
        (httpx.Response(503, text="maintenance"), "HTTP 503"),
        (httpx.ConnectError("connection refused"), "network error"),
        (httpx.Response(200, json=["2026-10-16T09:00:05"]), "expected a JSON object"),
        (httpx.Response(429), "rate-limited"),
    ],
)
def test_failing_version_endpoint_does_not_fail_the_request(
    api: TestClient,
    mock_api: respx.MockRouter,
    version_response: httpx.Response | Exception,
    cause: str,
) -> None:
    mock_api.get("/version").mock(side_effect=lambda request: raise_or_return(version_response))
    studies = load_fixture_studies("studies_pembrolizumab_page")
    mock_api.get("/studies").mock(return_value=studies_page(studies))
    response = api.post(
        "/query", json={"query": "q", "drug": "pembrolizumab", "dimension": "phase"}
    )
    assert response.status_code == 200, response.text
    body = VisualizationResponse.model_validate(response.json())
    assert body.meta.data_timestamp is None
    assert sum(d.count for d in body.visualization.data) == len(studies)
    (warning,) = [w for w in body.meta.warnings if "/version" in w]
    assert cause in warning
    assert "data_timestamp is null" in warning


def raise_or_return(outcome: httpx.Response | Exception) -> httpx.Response:
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def test_country_query_with_top_n(api: TestClient, mock_api: respx.MockRouter) -> None:
    studies = load_fixture_studies("studies_pembrolizumab_page")
    mock_api.get("/studies").mock(return_value=studies_page(studies))
    response = api.post(
        "/query",
        json={"query": "q", "condition": "melanoma", "dimension": "country", "top_n": 3},
    )
    assert response.status_code == 200, response.text
    body = VisualizationResponse.model_validate(response.json())
    known = [d for d in body.visualization.data if not d.is_unknown]
    assert len(known) == 3
    assert body.meta.top_n is not None
    assert body.meta.top_n.omitted_keys
    assert body.meta.buckets_overlap is True
    assert "condition" in body.meta.search_semantics


def test_empty_result_is_explained(api: TestClient, mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies").mock(return_value=studies_page([]))
    response = api.post("/query", json={"query": "q", "drug": "nothing", "dimension": "phase"})
    assert response.status_code == 200
    body = VisualizationResponse.model_validate(response.json())
    assert body.visualization.data == []
    assert "No trials matched these filters." in body.meta.warnings
    assert body.meta.coverage is None


def test_network_query(api: TestClient, mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies").mock(
        return_value=studies_page(load_fixture_studies("studies_pembrolizumab_page"))
    )
    response = api.post(
        "/query", json={"query": "q", "drug": "pembrolizumab", "relation": "sponsor_drug"}
    )
    assert response.status_code == 200, response.text
    body = VisualizationResponse.model_validate(response.json())
    assert body.visualization.type == "network_graph"
    assert body.visualization.nodes and body.visualization.edges


def test_plan_error_is_structured(api: TestClient, mock_api: respx.MockRouter) -> None:
    route = mock_api.get("/studies")
    response = api.post("/query", json={"query": "q", "drug": "pembrolizumab"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "no_dimension_or_relation"
    assert body["suggestions"]
    assert route.call_count == 0


def test_essie_syntax_rejected_before_any_request(
    api: TestClient, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("/studies")
    response = api.post(
        "/query", json={"query": "q", "condition": "lung OR breast", "dimension": "phase"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_filter_value"
    assert route.call_count == 0


def test_invalid_request_is_structured(api: TestClient) -> None:
    response = api.post("/query", json={"query": "q", "dimension": "colour"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    assert "dimension" in body["message"]


def test_upstream_rejection_carries_api_body(api: TestClient, mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies").mock(
        return_value=httpx.Response(400, text="Error parsing query in advanced filter")
    )
    response = api.post("/query", json={"query": "q", "drug": "x", "dimension": "phase"})
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_rejected_query"
    assert "Error parsing query" in response.json()["message"]


def test_rate_limit_is_503(api: TestClient, mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies").mock(return_value=httpx.Response(429))
    response = api.post("/query", json={"query": "q", "drug": "x", "dimension": "phase"})
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_rate_limited"


def test_health(api: TestClient) -> None:
    assert api.get("/health").json() == {"status": "ok"}


def test_openapi_docs_render(api: TestClient) -> None:
    schema = api.get("/openapi.json")
    assert schema.status_code == 200
    assert "/query" in schema.json()["paths"]
    assert "ErrorResponse" in schema.json()["components"]["schemas"]
    assert api.get("/docs").status_code == 200


def test_demo_ui_is_served_uncached(api: TestClient) -> None:
    response = api.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "/query" in response.text
    assert "/" not in api.get("/openapi.json").json()["paths"]
