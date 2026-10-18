"""Local response cache for API GETs, keyed by full URL + params.

Speeds up development and repeated questions; the TTL keeps it from outliving the
API's daily data refresh by much. Only successful JSON bodies are stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from ctviz.config import Settings

logger = logging.getLogger(__name__)

# A cached /studies page of 1000 records is several MB; decoding it takes tens of
# milliseconds, long enough to stall /health and other requests on the event loop.
LARGE_PAYLOAD_BYTES = 256 * 1024
# Seconds a connection waits for another writer's lock before raising, instead of
# failing at once when two requests store pages at the same time.
SQLITE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class CachedResponse:
    """A cached JSON body (an object, or a list for /studies/enums) and when it was stored."""

    payload: Any
    stored_at: datetime


class ResponseCache(Protocol):
    """Storage for successful JSON responses."""

    def get(self, key: str) -> CachedResponse | None:
        """Return a fresh entry, or None if absent or expired."""
        ...

    def set(self, key: str, payload: Any) -> None:
        """Store or replace an entry."""
        ...


def cache_key(url: str, params: Mapping[str, str]) -> str:
    """Stable key for a GET: the URL plus params sorted by name."""
    return f"{url}?{urlencode(sorted(params.items()))}"


class NoCache:
    """A cache that stores nothing; used in tests and when caching is disabled."""

    def get(self, key: str) -> CachedResponse | None:
        """Always a miss."""
        return None

    def set(self, key: str, payload: Any) -> None:
        """Discard the payload."""


class SqliteCache:
    """A single-file SQLite cache with a time-to-live.

    `clock` returns wall-clock seconds since the epoch; tests inject a fake one. The file
    uses write-ahead logging, so a reader never waits for a writer, and expired rows are
    deleted when the cache is opened, so the file does not grow without bound.
    """

    def __init__(
        self, path: Path, ttl_seconds: float, clock: Callable[[], float] = time.time
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(key TEXT PRIMARY KEY, stored_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._delete_expired()

    def _delete_expired(self) -> None:
        with self._connection:
            deleted = self._connection.execute(
                "DELETE FROM responses WHERE stored_at < ?", (self._clock() - self._ttl_seconds,)
            ).rowcount
        if deleted:
            logger.info("Response cache: deleted %d expired entries", deleted)

    def get(self, key: str) -> CachedResponse | None:
        """Return the entry if stored less than `ttl_seconds` ago."""
        row = self._fresh_row(key)
        if row is None:
            return None
        stored_at, payload = row
        return CachedResponse(payload=json.loads(payload), stored_at=stored_at)

    async def get_async(self, key: str) -> CachedResponse | None:
        """Like `get`, but a payload over LARGE_PAYLOAD_BYTES is decoded in a thread.

        The row is read on the event loop (an indexed lookup); only the JSON decoding,
        the slow part for a full page, moves off it.
        """
        row = self._fresh_row(key)
        if row is None:
            return None
        stored_at, payload = row
        if len(payload) > LARGE_PAYLOAD_BYTES:
            decoded = await asyncio.to_thread(json.loads, payload)
        else:
            decoded = json.loads(payload)
        return CachedResponse(payload=decoded, stored_at=stored_at)

    def _fresh_row(self, key: str) -> tuple[datetime, str] | None:
        row = self._connection.execute(
            "SELECT stored_at, payload FROM responses WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        stored_at, payload = row
        if self._clock() - stored_at > self._ttl_seconds:
            return None
        return datetime.fromtimestamp(stored_at, UTC), payload

    def set(self, key: str, payload: Any) -> None:
        """Store or replace an entry, stamped with the current time."""
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO responses (key, stored_at, payload) VALUES (?, ?, ?)",
                (key, self._clock(), json.dumps(payload)),
            )


async def get_cached(cache: ResponseCache, key: str) -> CachedResponse | None:
    """Look `key` up without decoding a large SQLite payload on the event loop."""
    if isinstance(cache, SqliteCache):
        return await cache.get_async(key)
    return cache.get(key)


def build_cache(settings: Settings) -> ResponseCache:
    """The cache configured by settings: SQLite under `cache_dir`, or none."""
    if not settings.cache_enabled:
        return NoCache()
    return SqliteCache(settings.cache_dir / "responses.sqlite", settings.cache_ttl_seconds)
