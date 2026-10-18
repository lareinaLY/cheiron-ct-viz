"""Unexpected failures and out-of-domain values still give a structured answer.

An unexpected exception becomes an ErrorResponse 500 with a logged traceback; a negative
or fractional enrollment count is kept as Unrecognized instead of crashing the enrollment
dimension; a non-transport httpx error (e.g. a decoding failure) is an upstream error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz import pipeline
from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.errors import UpstreamError
from ctviz.normalize import normalize_study
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.response import Datum, VisualizationResponse
from ctviz.schemas.trial import UnrecognizedValue
from tests.factories import SOURCE_PATHS, make_raw_study, set_path

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
        cache_enabled=False,
        enum_drift_check=False,
    )


def with_enrollment(nct_id: str, count: Any) -> dict[str, Any]:
    study = make_raw_study(nct_id, phases=["PHASE2"])
    set_path(study, SOURCE_PATHS["enrollment"], count)
    return study


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


def ask(body: dict[str, Any]) -> httpx.Response:
    with TestClient(create_app(settings()), raise_server_exceptions=False) as client:
        return client.post("/query", json=body)


class TestCatchAll:
    def test_unexpected_exception_is_a_structured_500(
        self,
        mock_api: respx.MockRouter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def explode(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(pipeline, "execute", explode)
        with caplog.at_level(logging.ERROR):
            response = ask({"query": "q", "drug": "pembrolizumab", "dimension": "phase"})

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error"
        assert "database is locked" not in body["message"]
        assert body["suggestions"]
        assert any(record.exc_info for record in caplog.records)


class TestEnrollmentOutOfDomain:
    @pytest.mark.parametrize(("count", "raw"), [(-1, "-1"), (12.5, "12.5")])
    def test_kept_as_unrecognized(self, count: Any, raw: str) -> None:
        record = normalize_study(with_enrollment("NCT1", count))
        assert record.enrollment == UnrecognizedValue(raw=raw)
        assert [u.record_field for u in record.unrecognized_values] == ["enrollment"]

    def test_integral_float_is_an_integer(self) -> None:
        assert normalize_study(with_enrollment("NCT1", 40.0)).enrollment == 40

    def test_poison_enrollment_does_not_fail_the_request(self, mock_api: respx.MockRouter) -> None:
        studies = [
            with_enrollment("NCT00000001", 30),
            with_enrollment("NCT00000002", 300),
            with_enrollment("NCT00000003", -1),
            with_enrollment("NCT00000004", 12.5),
        ]
        mock_api.get("/studies").mock(
            return_value=httpx.Response(200, json={"studies": studies, "totalCount": 4})
        )
        response = ask({"query": "q", "drug": "pembrolizumab", "dimension": "enrollment_bucket"})
        assert response.status_code == 200, response.text
        data = VisualizationResponse.model_validate(response.json()).visualization.data
        (unrecognized,) = [d for d in data if d.key == "Unrecognized"]
        assert isinstance(unrecognized, Datum)
        assert unrecognized.count == 2
        assert {c.value for c in unrecognized.citations} == {"-1", "12.5"}
        assert sum(d.count for d in data) == 4


class TestClientErrors:
    async def test_decoding_error_is_an_upstream_error(self, mock_api: respx.MockRouter) -> None:
        mock_api.get("/studies").mock(side_effect=httpx.DecodingError("bad gzip"))
        async with CtGovClient(settings(), cache=NoCache()) as client:
            with pytest.raises(UpstreamError):
                await client.fetch_all({})
