"""Local response cache for API GETs, keyed by full URL + params.

Speeds up development and repeated questions; the TTL keeps it from outliving the
API's daily data refresh by much. Only successful JSON bodies are stored.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from ctviz.config import Settings


@dataclass(frozen=True)
class CachedResponse:
    """A cached JSON body and when it was stored."""

    payload: dict[str, Any]
    stored_at: datetime


class ResponseCache(Protocol):
    """Storage for successful JSON responses."""

    def get(self, key: str) -> CachedResponse | None:
        """Return a fresh entry, or None if absent or expired."""
        ...

    def set(self, key: str, payload: dict[str, Any]) -> None:
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

    def set(self, key: str, payload: dict[str, Any]) -> None:
        """Discard the payload."""


class SqliteCache:
    """A single-file SQLite cache with a time-to-live.

    `clock` returns wall-clock seconds since the epoch; tests inject a fake one.
    """

    def __init__(
        self, path: Path, ttl_seconds: float, clock: Callable[[], float] = time.time
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(key TEXT PRIMARY KEY, stored_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def get(self, key: str) -> CachedResponse | None:
        """Return the entry if stored less than `ttl_seconds` ago."""
        row = self._connection.execute(
            "SELECT stored_at, payload FROM responses WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        stored_at, payload = row
        if self._clock() - stored_at > self._ttl_seconds:
            return None
        return CachedResponse(
            payload=json.loads(payload), stored_at=datetime.fromtimestamp(stored_at, UTC)
        )

    def set(self, key: str, payload: dict[str, Any]) -> None:
        """Store or replace an entry, stamped with the current time."""
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO responses (key, stored_at, payload) VALUES (?, ?, ?)",
                (key, self._clock(), json.dumps(payload)),
            )


def build_cache(settings: Settings) -> ResponseCache:
    """The cache configured by settings: SQLite under `cache_dir`, or none."""
    if not settings.cache_enabled:
        return NoCache()
    return SqliteCache(settings.cache_dir / "responses.sqlite", settings.cache_ttl_seconds)
