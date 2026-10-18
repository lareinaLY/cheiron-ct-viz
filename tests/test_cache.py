"""Tests for retrieval.cache."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ctviz.retrieval.cache import NoCache, SqliteCache, cache_key


class FakeWallClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_key_covers_url_and_params_regardless_of_order() -> None:
    first = cache_key("https://x/studies", {"a": "1", "b": "2"})
    assert first == cache_key("https://x/studies", {"b": "2", "a": "1"})
    assert first != cache_key("https://x/studies", {"a": "1", "b": "3"})
    assert first != cache_key("https://x/version", {"a": "1", "b": "2"})


def test_no_cache_never_returns_anything() -> None:
    cache = NoCache()
    cache.set("k", {"x": 1})
    assert cache.get("k") is None


def test_sqlite_round_trip_with_stored_at(tmp_path: Path) -> None:
    clock = FakeWallClock()
    cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock)
    cache.set("k", {"studies": [{"a": 1}], "totalCount": 1})
    hit = cache.get("k")
    assert hit is not None
    assert hit.payload == {"studies": [{"a": 1}], "totalCount": 1}
    assert hit.stored_at == datetime.fromtimestamp(clock.now, UTC)


def test_sqlite_entry_expires_after_ttl(tmp_path: Path) -> None:
    clock = FakeWallClock()
    cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock)
    cache.set("k", {"x": 1})
    clock.now += 59
    assert cache.get("k") is not None
    clock.now += 2
    assert cache.get("k") is None


def test_sqlite_persists_across_instances(tmp_path: Path) -> None:
    clock = FakeWallClock()
    SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock).set("k", {"x": 1})
    assert SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock).get("k") is not None


def test_sqlite_overwrite_refreshes_entry(tmp_path: Path) -> None:
    clock = FakeWallClock()
    cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=60, clock=clock)
    cache.set("k", {"x": 1})
    clock.now += 50
    cache.set("k", {"x": 2})
    clock.now += 50
    hit = cache.get("k")
    assert hit is not None
    assert hit.payload == {"x": 2}
