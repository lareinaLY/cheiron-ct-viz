"""Tests for retrieval.client (pagination, reconciliation, pacing, retries, caching)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx

from ctviz.config import Settings
from ctviz.errors import ApiRequestError, RateLimitError, UpstreamError
from ctviz.retrieval.cache import CachedResponse, NoCache, ResponseCache
from ctviz.retrieval.client import CtGovClient, TokenBucket
from ctviz.retrieval.fields import STUDY_FIELDS

BASE_URL = "https://ctgov.test/api/v2"


class FakeClock:
    """Monotonic clock whose sleep advances time instantly and records each wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


class DictCache:
    """In-memory ResponseCache test double whose entries can be removed."""

    def __init__(self) -> None:
        self.entries: dict[str, CachedResponse] = {}

    def get(self, key: str) -> CachedResponse | None:
        return self.entries.get(key)

    def set(self, key: str, payload: dict[str, Any]) -> None:
        self.entries[key] = CachedResponse(payload=payload, stored_at=datetime(2026, 10, 17, 12))


def study(nct_id: str) -> dict[str, Any]:
    return {"protocolSection": {"identificationModule": {"nctId": nct_id}}}


def page(ids: list[str], total: int | None = None, token: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"studies": [study(i) for i in ids]}
    if total is not None:
        body["totalCount"] = total
    if token is not None:
        body["nextPageToken"] = token
    return httpx.Response(200, json=body)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ctgov_base_url": BASE_URL,
        "min_request_interval_seconds": 0.0,
        "backoff_base_seconds": 1.0,
        "max_retries": 3,
        "cache_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


def make_client(
    clock: FakeClock,
    *,
    cache: ResponseCache | None = None,
    jitter: float = 0.0,
    **overrides: Any,
) -> CtGovClient:
    return CtGovClient(
        settings(**overrides),
        cache=cache or NoCache(),
        clock=clock,
        sleep=clock.sleep,
        random=lambda: jitter,
    )


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[CtGovClient]:
    async with make_client(clock) as instance:
        yield instance


def sent_params(route: respx.Route) -> list[dict[str, str]]:
    return [dict(call.request.url.params) for call in route.calls]


class TestPagination:
    async def test_first_page_sends_projection_and_count_total(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(return_value=page(["NCT1"], total=1))
        await client.fetch_all({"query.intr": "pembrolizumab"})
        (params,) = sent_params(route)
        assert params == {
            "query.intr": "pembrolizumab",
            "fields": "|".join(STUDY_FIELDS),
            "countTotal": "true",
            "pageSize": "1000",
        }

    async def test_follows_token_with_frozen_params(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(
            side_effect=[
                page(["NCT1"], total=3, token="t1"),
                page(["NCT2"], token="t2"),
                page(["NCT3"]),
            ]
        )
        result = await client.fetch_all({"query.cond": "melanoma"})
        params = sent_params(route)
        assert [p.pop("pageToken", None) for p in params] == [None, "t1", "t2"]
        assert params[0] == params[1] == params[2]
        assert result.pages == 3
        assert [s["protocolSection"]["identificationModule"]["nctId"] for s in result.studies] == [
            "NCT1",
            "NCT2",
            "NCT3",
        ]

    async def test_page_size_clamped_to_api_maximum(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(return_value=page([], total=0))
        async with make_client(clock, page_size=5000) as client:
            await client.fetch_all({})
        assert sent_params(route)[0]["pageSize"] == "1000"

    async def test_page_size_never_exceeds_record_cap(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(return_value=page([], total=0))
        async with make_client(clock, max_records=250) as client:
            await client.fetch_all({})
        assert sent_params(route)[0]["pageSize"] == "250"

    async def test_empty_page_with_token_is_followed(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(side_effect=[page([], total=1, token="t1"), page(["NCT1"])])
        result = await client.fetch_all({})
        assert len(result.studies) == 1
        assert result.reconciled is True


class TestReconciliation:
    async def test_match(self, client: CtGovClient, mock_api: respx.MockRouter) -> None:
        mock_api.get("/studies").mock(
            side_effect=[page(["NCT1", "NCT2"], total=3, token="t1"), page(["NCT3"])]
        )
        result = await client.fetch_all({})
        assert result.total_count == 3
        assert len(result.studies) == 3
        assert result.reconciled is True
        assert result.truncated is False
        assert result.coverage == 1.0

    async def test_mismatch_is_flagged_not_hidden(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(return_value=page(["NCT1", "NCT2", "NCT3"], total=5))
        result = await client.fetch_all({})
        assert len(result.studies) == 3
        assert result.total_count == 5
        assert result.reconciled is False
        assert result.truncated is False
        assert result.coverage == 0.6

    async def test_duplicates_across_pages_dropped_and_counted(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(
            side_effect=[page(["NCT1", "NCT2"], total=3, token="t1"), page(["NCT2", "NCT3"])]
        )
        result = await client.fetch_all({})
        assert len(result.studies) == 3
        assert result.duplicates_dropped == 1
        assert result.reconciled is True

    async def test_zero_matches(self, client: CtGovClient, mock_api: respx.MockRouter) -> None:
        mock_api.get("/studies").mock(return_value=page([], total=0))
        result = await client.fetch_all({})
        assert result.studies == []
        assert result.reconciled is True
        assert result.coverage is None

    async def test_missing_total_count_is_an_upstream_error(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(return_value=page(["NCT1"]))
        with pytest.raises(UpstreamError, match="totalCount"):
            await client.fetch_all({})


class TestCap:
    async def test_stops_at_cap_and_reports_coverage(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(
            side_effect=[
                page(["NCT1", "NCT2"], total=10, token="t1"),
                page(["NCT3", "NCT4"], token="t2"),
                page(["NCT5", "NCT6"], token="t3"),
            ]
        )
        async with make_client(clock, max_records=4, page_size=2) as client:
            result = await client.fetch_all({})
        assert route.call_count == 2
        assert len(result.studies) == 4
        assert result.truncated is True
        assert result.coverage == 0.4
        assert result.reconciled is True

    async def test_cap_inside_a_page_trims_the_page(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(
            side_effect=[page(["NCT1", "NCT2"], total=10, token="t1"), page(["NCT3", "NCT4"])]
        )
        async with make_client(clock, max_records=3, page_size=2) as client:
            result = await client.fetch_all({})
        assert len(result.studies) == 3
        assert result.truncated is True

    async def test_total_equal_to_cap_is_not_truncated(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(return_value=page(["NCT1", "NCT2"], total=2))
        async with make_client(clock, max_records=2) as client:
            result = await client.fetch_all({})
        assert result.truncated is False
        assert result.reconciled is True


class TestTokenBucket:
    async def test_first_request_immediate_then_paced(self, clock: FakeClock) -> None:
        bucket = TokenBucket(interval_seconds=1.2, clock=clock, sleep=clock.sleep)
        for _ in range(3):
            await bucket.acquire()
        assert clock.sleeps == [1.2, 1.2]

    async def test_idle_time_is_not_banked_beyond_one_request(self, clock: FakeClock) -> None:
        bucket = TokenBucket(interval_seconds=1.2, clock=clock, sleep=clock.sleep)
        await bucket.acquire()
        clock.now += 60
        await bucket.acquire()
        await bucket.acquire()
        assert clock.sleeps == [1.2]

    async def test_partial_wait(self, clock: FakeClock) -> None:
        bucket = TokenBucket(interval_seconds=1.2, clock=clock, sleep=clock.sleep)
        await bucket.acquire()
        clock.now += 0.5
        await bucket.acquire()
        assert clock.sleeps == [0.7]

    async def test_client_paces_page_requests(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(
            side_effect=[page(["NCT1"], total=2, token="t1"), page(["NCT2"])]
        )
        async with make_client(clock, min_request_interval_seconds=1.2) as client:
            await client.fetch_all({})
        assert clock.sleeps == [1.2]


class TestRetries:
    async def test_429_backs_off_exponentially_then_succeeds(
        self, client: CtGovClient, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(
            side_effect=[
                httpx.Response(429, text="<html>429 Too Many Requests</html>"),
                httpx.Response(429, text="<html>429 Too Many Requests</html>"),
                page(["NCT1"], total=1),
            ]
        )
        result = await client.fetch_all({})
        assert len(result.studies) == 1
        assert clock.sleeps == [1.0, 2.0]

    async def test_backoff_adds_jitter(self, clock: FakeClock, mock_api: respx.MockRouter) -> None:
        mock_api.get("/studies").mock(
            side_effect=[httpx.Response(429), httpx.Response(429), page([], total=0)]
        )
        async with make_client(clock, jitter=0.5) as client:
            await client.fetch_all({})
        assert clock.sleeps == [1.5, 2.5]

    async def test_429_exhausted_raises_rate_limit_error(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(return_value=httpx.Response(429))
        async with make_client(clock, max_retries=2) as client:
            with pytest.raises(RateLimitError):
                await client.fetch_all({})
        assert route.call_count == 3
        assert clock.sleeps == [1.0, 2.0]

    async def test_5xx_is_retried(self, client: CtGovClient, mock_api: respx.MockRouter) -> None:
        mock_api.get("/studies").mock(
            side_effect=[httpx.Response(503, text="unavailable"), page(["NCT1"], total=1)]
        )
        result = await client.fetch_all({})
        assert len(result.studies) == 1

    async def test_5xx_exhausted_raises_upstream_error(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(return_value=httpx.Response(502, text="bad gw"))
        async with make_client(clock, max_retries=1) as client:
            with pytest.raises(UpstreamError) as error:
                await client.fetch_all({})
        assert error.value.status_code == 502
        assert route.call_count == 2

    async def test_network_error_is_retried(
        self, client: CtGovClient, mock_api: respx.MockRouter
    ) -> None:
        mock_api.get("/studies").mock(
            side_effect=[httpx.ConnectError("boom"), page(["NCT1"], total=1)]
        )
        result = await client.fetch_all({})
        assert len(result.studies) == 1

    async def test_4xx_fails_fast_with_body(
        self, client: CtGovClient, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        body = "Parameter 'fields' contains invalid field name: 'NotAField'"
        route = mock_api.get("/studies").mock(return_value=httpx.Response(400, text=body))
        with pytest.raises(ApiRequestError) as error:
            await client.fetch_all({})
        assert error.value.status_code == 400
        assert body in str(error.value)
        assert route.call_count == 1
        assert clock.sleeps == []


class TestVersion:
    async def test_data_timestamp(self, client: CtGovClient, mock_api: respx.MockRouter) -> None:
        mock_api.get("/version").mock(
            return_value=httpx.Response(
                200, json={"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
            )
        )
        assert await client.data_timestamp() == datetime(2026, 10, 16, 9, 0, 5)

    async def test_data_timestamp_reused_within_ttl(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/version").mock(
            return_value=httpx.Response(200, json={"dataTimestamp": "2026-10-16T09:00:05"})
        )
        async with make_client(clock, version_ttl_seconds=600) as client:
            await client.data_timestamp()
            clock.now += 599
            await client.data_timestamp()
            assert route.call_count == 1
            clock.now += 2
            await client.data_timestamp()
            assert route.call_count == 2


class TestCache:
    async def test_second_fetch_served_from_cache(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.get("/studies").mock(
            side_effect=[page(["NCT1"], total=2, token="t1"), page(["NCT2"])]
        )
        async with make_client(clock, cache=DictCache()) as client:
            live = await client.fetch_all({"query.cond": "x"})
            cached = await client.fetch_all({"query.cond": "x"})
        assert route.call_count == 2
        assert live.cached_at is None
        assert cached.cached_at == datetime(2026, 10, 17, 12)
        assert cached.studies == live.studies

    async def test_partial_cache_hit_refetches_everything_live(
        self, clock: FakeClock, mock_api: respx.MockRouter
    ) -> None:
        # A cached page 1 with an uncached page 2 would mix tokens from different
        # fetches; tokens are not bound to the query, so the client restarts live.
        route = mock_api.get("/studies").mock(
            side_effect=[
                page(["NCT1"], total=2, token="t1"),
                page(["NCT2"]),
                page(["NCT1"], total=2, token="t9"),
                page(["NCT2"]),
            ]
        )
        cache = DictCache()
        async with make_client(clock, cache=cache) as client:
            await client.fetch_all({})
            page_two_key = next(k for k in cache.entries if "pageToken=t1" in k)
            del cache.entries[page_two_key]
            result = await client.fetch_all({})
        assert route.call_count == 4
        assert sent_params(route)[3]["pageToken"] == "t9"
        assert result.cached_at is None
        assert len(result.studies) == 2
