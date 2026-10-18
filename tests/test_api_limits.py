"""Request deadline, concurrency cap, demo token, bounded skipped-record warnings."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz import pipeline
from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.schemas.response import NoticeCode, VisualizationResponse
from tests.factories import make_raw_study, set_path

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
STRUCTURED = {"query": "q", "drug": "pembrolizumab", "dimension": "country"}


def settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
        cache_enabled=False,
        enum_drift_check=False,
        **overrides,
    )


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


def serve(router: respx.MockRouter, studies: list[dict[str, Any]]) -> None:
    router.get("/studies").mock(
        return_value=httpx.Response(200, json={"studies": studies, "totalCount": len(studies)})
    )


def test_defaults() -> None:
    defaults = settings()
    assert defaults.request_deadline_seconds == 120
    assert defaults.max_concurrent_queries == 4
    assert defaults.demo_token is None


def test_request_over_the_deadline_is_a_504(
    mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(*args: Any, **kwargs: Any) -> VisualizationResponse:
        await asyncio.sleep(5)
        raise AssertionError("the deadline should have cancelled this")

    monkeypatch.setattr(pipeline, "execute", slow)
    with TestClient(create_app(settings(request_deadline_seconds=0.05))) as api:
        response = api.post("/query", json=STRUCTURED)
    assert response.status_code == 504
    body = response.json()
    assert body["code"] == "request_timeout"
    assert "0.05" in body["message"]


class TestDemoToken:
    def app(self) -> TestClient:
        return TestClient(create_app(settings(demo_token="s3cret")))

    def test_missing_token_is_401(self, mock_api: respx.MockRouter) -> None:
        with self.app() as api:
            response = api.post("/query", json=STRUCTURED)
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"

    def test_wrong_token_is_401(self, mock_api: respx.MockRouter) -> None:
        with self.app() as api:
            response = api.post("/query", json=STRUCTURED, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_bearer_header_is_accepted(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, [make_raw_study("NCT1", countries=["France"])])
        with self.app() as api:
            response = api.post(
                "/query", json=STRUCTURED, headers={"Authorization": "Bearer s3cret"}
            )
        assert response.status_code == 200

    def test_query_parameter_is_accepted(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, [make_raw_study("NCT1", countries=["France"])])
        with self.app() as api:
            response = api.post("/query?token=s3cret", json=STRUCTURED)
        assert response.status_code == 200

    def test_health_and_ui_need_no_token(self) -> None:
        with self.app() as api:
            assert api.get("/health").status_code == 200
            assert api.get("/").status_code == 200

    def test_no_token_configured_means_no_check(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, [make_raw_study("NCT1", countries=["France"])])
        with TestClient(create_app(settings())) as api:
            assert api.post("/query", json=STRUCTURED).status_code == 200


def test_long_query_is_a_422(mock_api: respx.MockRouter) -> None:
    with TestClient(create_app(settings())) as api:
        response = api.post("/query", json={**STRUCTURED, "query": "x" * 2001})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_skipped_records_are_listed_ten_at_a_time(mock_api: respx.MockRouter) -> None:
    good = [make_raw_study(f"NCT1{i:07d}", countries=["France"]) for i in range(400)]
    bad = []
    for i in range(15):
        study = make_raw_study(f"NCT2{i:07d}")
        set_path(study, "protocolSection.designModule.phases", "PHASE9")
        bad.append(study)
    serve(mock_api, [*good, *bad])
    with TestClient(create_app(settings())) as api:
        body = VisualizationResponse.model_validate(api.post("/query", json=STRUCTURED).json())
    skipped = [w for w in body.meta.warnings if w.code == NoticeCode.SKIPPED_RECORDS]
    listed = [w for w in skipped if w.message.startswith("Skipped ")]
    assert len(listed) == 10
    assert skipped[-1].message == (
        "... and 5 more skipped records (the full list is in the server log)."
    )
