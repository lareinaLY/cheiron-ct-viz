"""Degradation thresholds: skipped (unreadable) records make a result degraded or refused."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ctviz import pipeline
from ctviz.api.app import create_app
from ctviz.config import DataQualityThresholds, Settings
from ctviz.errors import UpstreamDataFormatError
from ctviz.pipeline import SkipBand, classify_skip_rate
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import ErrorResponse, VisualizationResponse
from tests.factories import make_raw_study, messages

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
DEFAULTS = DataQualityThresholds()


def unreadable(index: int) -> dict[str, Any]:
    """A structurally broken study: protocolSection without an NCT ID."""
    return {"protocolSection": {"designModule": {"phases": ["PHASE2"]}}, "_index": index}


def batch(good: int, skipped: int) -> list[dict[str, Any]]:
    studies = [make_raw_study(f"NCT{index:08d}", phases=["PHASE2"]) for index in range(good)]
    return studies + [unreadable(index) for index in range(skipped)]


class TestClassifySkipRate:
    def test_defaults(self) -> None:
        assert DEFAULTS.degraded_skip_ratio == 0.05
        assert DEFAULTS.refuse_skip_ratio == 0.5
        assert DEFAULTS.min_skipped_for_degraded == 20

    def test_nothing_skipped_is_normal(self) -> None:
        assert classify_skip_rate(2575, 0, DEFAULTS) == SkipBand.NORMAL

    def test_below_degraded_ratio_is_normal(self) -> None:
        assert classify_skip_rate(1000, 49, DEFAULTS) == SkipBand.NORMAL

    def test_at_degraded_ratio_and_min_count_is_degraded(self) -> None:
        assert classify_skip_rate(400, 20, DEFAULTS) == SkipBand.DEGRADED

    def test_between_ratios_is_degraded(self) -> None:
        assert classify_skip_rate(1000, 300, DEFAULTS) == SkipBand.DEGRADED

    def test_at_refuse_ratio_is_refused(self) -> None:
        assert classify_skip_rate(40, 20, DEFAULTS) == SkipBand.REFUSE

    def test_everything_skipped_is_refused(self) -> None:
        assert classify_skip_rate(2575, 2575, DEFAULTS) == SkipBand.REFUSE

    def test_small_sample_is_normal_below_min_skipped(self) -> None:
        assert classify_skip_rate(10, 3, DEFAULTS) == SkipBand.NORMAL

    def test_high_ratio_below_min_skipped_is_normal(self) -> None:
        assert classify_skip_rate(30, 19, DEFAULTS) == SkipBand.NORMAL

    def test_ratio_above_degraded_below_min_count_is_normal(self) -> None:
        assert classify_skip_rate(200, 19, DEFAULTS) == SkipBand.NORMAL

    def test_nothing_retrieved_is_normal(self) -> None:
        assert classify_skip_rate(0, 0, DEFAULTS) == SkipBand.NORMAL

    def test_custom_thresholds(self) -> None:
        strict = DataQualityThresholds(
            degraded_skip_ratio=0.01, refuse_skip_ratio=0.1, min_skipped_for_degraded=1
        )
        assert classify_skip_rate(100, 1, strict) == SkipBand.DEGRADED
        assert classify_skip_rate(100, 10, strict) == SkipBand.REFUSE


class TestSettings:
    def test_thresholds_read_from_environment_names(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            DEGRADED_SKIP_RATIO=0.1,
            REFUSE_SKIP_RATIO=0.6,
            MIN_SKIPPED_FOR_DEGRADED=5,
        )
        assert settings.data_quality_thresholds() == DataQualityThresholds(
            degraded_skip_ratio=0.1, refuse_skip_ratio=0.6, min_skipped_for_degraded=5
        )

    def test_refuse_ratio_below_degraded_ratio_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="REFUSE_SKIP_RATIO"):
            DataQualityThresholds(degraded_skip_ratio=0.5, refuse_skip_ratio=0.1)


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


@pytest.fixture
def api(mock_api: respx.MockRouter) -> Iterator[TestClient]:
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


def serve(mock_api: respx.MockRouter, studies: list[dict[str, Any]]) -> None:
    body = {"studies": studies, "totalCount": len(studies)}
    mock_api.get("/studies").mock(return_value=httpx.Response(200, json=body))


def ask(api: TestClient, **fields: Any) -> httpx.Response:
    return api.post(
        "/query", json={"query": "q", "drug": "pembrolizumab", "dimension": "phase", **fields}
    )


class TestBandsEndToEnd:
    def test_normal(self, api: TestClient, mock_api: respx.MockRouter) -> None:
        serve(mock_api, batch(good=20, skipped=0))
        meta = VisualizationResponse.model_validate(ask(api).json()).meta
        assert meta.data_quality == "normal"
        assert not any("DEGRADED" in w for w in messages(meta.warnings))

    def test_small_sample_stays_normal(self, api: TestClient, mock_api: respx.MockRouter) -> None:
        serve(mock_api, batch(good=7, skipped=3))
        response = ask(api)
        assert response.status_code == 200, response.text
        meta = VisualizationResponse.model_validate(response.json()).meta
        assert meta.data_quality == "normal"
        assert meta.cohorts[0].records_skipped == 3
        assert any("3 of 10 retrieved trials were skipped" in w for w in messages(meta.warnings))

    def test_degraded(self, api: TestClient, mock_api: respx.MockRouter) -> None:
        serve(mock_api, batch(good=380, skipped=20))
        response = ask(api)
        assert response.status_code == 200, response.text
        body = VisualizationResponse.model_validate(response.json())
        assert body.meta.data_quality == "degraded"
        first = messages(body.meta.warnings)[0]
        assert first.startswith("DEGRADED DATA:")
        assert "20 of 400" in first
        assert "5.0%" in first
        assert sum(d.count for d in body.visualization.data) == 380

    def test_refused_with_structured_error(
        self, api: TestClient, mock_api: respx.MockRouter
    ) -> None:
        serve(mock_api, batch(good=20, skipped=30))
        response = ask(api)
        assert response.status_code == 502
        error = ErrorResponse.model_validate(response.json())
        assert error.code == "upstream_data_format_changed"
        assert error.message.startswith(
            "upstream data format may have changed; refusing to return a chart that could mislead"
        )
        assert "30 of 50" in error.message
        assert error.suggestions

    async def test_any_refused_cohort_refuses_the_comparison(
        self, mock_api: respx.MockRouter
    ) -> None:
        good = {"studies": batch(good=50, skipped=0), "totalCount": 50}
        bad = {"studies": batch(good=5, skipped=25), "totalCount": 30}
        mock_api.get("/studies").mock(
            side_effect=[httpx.Response(200, json=good), httpx.Response(200, json=bad)]
        )
        plan = QueryPlan(
            cohorts=[
                Cohort(label="a", filters=Filters(drug="pembrolizumab")),
                Cohort(label="b", filters=Filters(drug="nivolumab")),
            ],
            dimension=Dimension.PHASE,
        )
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, ctgov_base_url=BASE_URL, min_request_interval_seconds=0
        )
        async with CtGovClient(settings, cache=NoCache()) as client:
            with pytest.raises(UpstreamDataFormatError) as caught:
                await pipeline.run(plan, question="q", client=client)
        assert caught.value.cohort == "b"
        assert (caught.value.retrieved, caught.value.skipped) == (30, 25)

    def test_skip_rate_is_logged_per_cohort(
        self, api: TestClient, mock_api: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        serve(mock_api, batch(good=7, skipped=3))
        with caplog.at_level(logging.INFO, logger="ctviz.pipeline"):
            ask(api)
        lines = [r.getMessage() for r in caplog.records if "skip_ratio=" in r.getMessage()]
        assert len(lines) == 1
        assert "retrieved=10" in lines[0]
        assert "skipped=3" in lines[0]
        assert "skip_ratio=0.3000" in lines[0]
