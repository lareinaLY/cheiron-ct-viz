"""Tests for the stability bundle: gzip, the pagination page cap, explicit internal checks,
SQLite cache hygiene and the startup /version self-check."""

from __future__ import annotations

import ast
import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import ctviz
from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.retrieval import cache as cache_module
from ctviz.retrieval.cache import LARGE_PAYLOAD_BYTES, NoCache, SqliteCache
from ctviz.retrieval.client import CtGovClient, check_api_reachable, max_pages
from tests.factories import load_fixture_studies

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ctgov_base_url": BASE_URL,
        "min_request_interval_seconds": 0.0,
        "backoff_base_seconds": 0.0,
        "max_retries": 0,
        "cache_enabled": False,
        "enum_drift_check": False,
        "startup_self_check": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def study(nct_id: str) -> dict[str, Any]:
    return {"protocolSection": {"identificationModule": {"nctId": nct_id}}}


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


class TestGzip:
    def test_large_response_is_gzipped(self, mock_api: respx.MockRouter) -> None:
        studies = load_fixture_studies("studies_pembrolizumab_page")
        mock_api.get("/studies").mock(
            return_value=httpx.Response(200, json={"studies": studies, "totalCount": len(studies)})
        )
        with TestClient(create_app(settings())) as api:
            response = api.post(
                "/query",
                json={"query": "q", "drug": "pembrolizumab", "dimension": "phase"},
                headers={"Accept-Encoding": "gzip"},
            )
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert response.json()["visualization"]["type"] == "bar_chart"

    def test_small_response_is_not_gzipped(self) -> None:
        with TestClient(create_app(settings())) as api:
            response = api.get("/health", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in response.headers


class TestPageCap:
    def test_page_cap_allows_two_pages_beyond_the_record_cap(self) -> None:
        assert max_pages(max_records=10_000, page_size=1000) == 12
        assert max_pages(max_records=10, page_size=3) == 6

    async def test_endless_tokens_stop_at_the_cap(self, mock_api: respx.MockRouter) -> None:
        route = mock_api.get("/studies").mock(
            side_effect=[
                httpx.Response(
                    200, json={"studies": [study("NCT1")], "totalCount": 2, "nextPageToken": "t"}
                ),
                *[
                    httpx.Response(200, json={"studies": [], "nextPageToken": "t"})
                    for _ in range(20)
                ],
            ]
        )
        async with CtGovClient(settings(max_records=2, page_size=1), cache=NoCache()) as client:
            result = await client.fetch_all({})
        assert route.call_count == 4
        assert result.pages == 4
        assert result.page_cap_hit is True
        assert result.reconciled is False

    async def test_normal_fetch_does_not_hit_the_cap(self, mock_api: respx.MockRouter) -> None:
        mock_api.get("/studies").mock(
            return_value=httpx.Response(200, json={"studies": [study("NCT1")], "totalCount": 1})
        )
        async with CtGovClient(settings(), cache=NoCache()) as client:
            result = await client.fetch_all({})
        assert result.page_cap_hit is False

    def test_cap_is_reported_as_a_warning(self, mock_api: respx.MockRouter) -> None:
        studies = load_fixture_studies("studies_pembrolizumab_page")
        mock_api.get("/studies").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"studies": studies[:1], "totalCount": 5, "nextPageToken": "t"},
                ),
                *[
                    httpx.Response(200, json={"studies": [], "nextPageToken": "t"})
                    for _ in range(20)
                ],
            ]
        )
        with TestClient(create_app(settings(max_records=5, page_size=5))) as api:
            response = api.post(
                "/query", json={"query": "q", "drug": "pembrolizumab", "dimension": "phase"}
            )
        assert response.status_code == 200
        warnings = response.json()["meta"]["warnings"]
        cap = [w for w in warnings if w["code"] == "pagination_cap"]
        assert len(cap) == 1
        assert cap[0]["severity"] == "warning"
        assert "3 pages" in cap[0]["message"]


def test_source_has_no_assert_statements() -> None:
    # `python -O` strips asserts, so internal checks must be explicit (bandit B101).
    root = Path(ctviz.__file__).parent
    found = [
        f"{path.relative_to(root)}:{node.lineno}"
        for path in sorted(root.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Assert)
    ]
    assert found == []


class FakeWallClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestSqliteCache:
    def test_uses_write_ahead_logging(self, tmp_path: Path) -> None:
        SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60)
        with sqlite3.connect(tmp_path / "c.sqlite") as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_expired_rows_are_deleted_at_startup(self, tmp_path: Path) -> None:
        clock = FakeWallClock()
        first = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock)
        first.set("old", {"x": 1})
        clock.now += 30
        first.set("fresh", {"x": 2})
        clock.now += 40
        SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock)
        with sqlite3.connect(tmp_path / "c.sqlite") as connection:
            keys = [row[0] for row in connection.execute("SELECT key FROM responses")]
        assert keys == ["fresh"]

    async def test_large_payload_is_decoded_in_a_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        threaded: list[str] = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(function: Any, *args: Any) -> Any:
            threaded.append(function.__name__)
            return await real_to_thread(function, *args)

        monkeypatch.setattr(cache_module.asyncio, "to_thread", recording_to_thread)
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60)
        large = {"studies": ["x" * LARGE_PAYLOAD_BYTES]}
        cache.set("large", large)
        cache.set("small", {"studies": []})
        assert (await cache.get_async("small")).payload == {"studies": []}  # type: ignore[union-attr]
        assert threaded == []
        assert (await cache.get_async("large")).payload == large  # type: ignore[union-attr]
        assert threaded == ["loads"]

    async def test_get_async_misses_like_get(self, tmp_path: Path) -> None:
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60)
        assert await cache.get_async("absent") is None


class TestStartupSelfCheck:
    async def test_reachable_api_logs_info(
        self, mock_api: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="ctviz.retrieval")
        async with CtGovClient(settings(), cache=NoCache()) as client:
            assert await check_api_reachable(client) is True
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.parametrize(
        "outcome", [httpx.Response(503, text="down"), httpx.ConnectError("refused")]
    )
    async def test_unreachable_api_logs_one_warning(
        self,
        mock_api: respx.MockRouter,
        caplog: pytest.LogCaptureFixture,
        outcome: httpx.Response | Exception,
    ) -> None:
        route = mock_api.get("/version").mock(side_effect=[outcome])
        async with CtGovClient(settings(max_retries=3), cache=NoCache()) as client:
            assert await check_api_reachable(client) is False
        assert route.call_count == 1
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "unreachable" in warnings[0]

    def test_startup_is_not_blocked_by_a_hanging_api(self, mock_api: respx.MockRouter) -> None:
        async def hang(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(60)
            return httpx.Response(200, json=VERSION)

        mock_api.get("/version").mock(side_effect=hang)
        with TestClient(create_app(settings(startup_self_check=True))) as api:
            assert api.get("/health").status_code == 200
            assert api.app.state.self_check_task is not None  # type: ignore[attr-defined]
