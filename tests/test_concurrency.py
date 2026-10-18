"""Concurrent requests share one rate limiter: upstream calls stay paced across requests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from itertools import pairwise

import httpx
import pytest
import respx

from ctviz.api.app import create_app
from ctviz.config import Settings
from tests.factories import make_raw_study

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
INTERVAL_SECONDS = 0.1
# Event-loop scheduling between the limiter granting a slot and the mock recording the
# call; far below the interval, so a per-request limiter (gaps near 0) still fails.
SCHEDULING_SLACK_SECONDS = 0.02
DRUGS = [f"drug{index}" for index in range(10)]


@pytest.fixture
def recorded_calls() -> Iterator[list[tuple[float, str]]]:
    """Mock the API and record (monotonic time, path) for every upstream call."""
    calls: list[tuple[float, str]] = []

    def record(request: httpx.Request, payload: dict[str, object]) -> httpx.Response:
        calls.append((time.monotonic(), request.url.path))
        return httpx.Response(200, json=payload)

    page = {"studies": [make_raw_study("NCT00000001")], "totalCount": 1}
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/version").mock(side_effect=lambda request: record(request, VERSION))
        router.get("/studies").mock(side_effect=lambda request: record(request, page))
        yield calls


async def test_concurrent_requests_are_paced_by_one_shared_limiter(
    recorded_calls: list[tuple[float, str]],
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=INTERVAL_SECONDS,
        cache_enabled=False,
        enum_drift_check=False,
        openai_api_key=None,
        deepseek_api_key=None,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ctviz") as api,
    ):
        responses = await asyncio.gather(
            *(
                api.post("/query", json={"query": "q", "drug": drug, "dimension": "phase"})
                for drug in DRUGS
            )
        )

    assert [response.status_code for response in responses] == [200] * len(DRUGS)
    studies_calls = [call for call in recorded_calls if call[1].endswith("/studies")]
    assert len(studies_calls) == len(DRUGS)
    times = sorted(at for at, _ in recorded_calls)
    gaps = [later - earlier for earlier, later in pairwise(times)]
    assert min(gaps) >= INTERVAL_SECONDS - SCHEDULING_SLACK_SECONDS, gaps
    # One limiter for the app: all calls in one paced sequence, not ten parallel ones.
    assert times[-1] - times[0] >= (len(times) - 1) * (INTERVAL_SECONDS - SCHEDULING_SLACK_SECONDS)
