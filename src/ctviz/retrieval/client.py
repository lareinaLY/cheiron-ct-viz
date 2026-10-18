"""Async ClinicalTrials.gov API v2 client: paced, retrying, paginating, reconciling.

API behaviors this client is built around (docs/api_findings.md §6, §11):
- pageSize is silently capped at 1000; totalCount comes only on the first page;
- nextPageToken is not bound to the query, so every page resends identical params;
- bursts get HTML 429 with no Retry-After, so requests are paced client-side.
"""

from __future__ import annotations

import asyncio
import logging
import random as random_module
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx

from ctviz.config import Settings
from ctviz.errors import ApiRequestError, RateLimitError, UpstreamError
from ctviz.retrieval.cache import ResponseCache, cache_key
from ctviz.retrieval.fields import STUDY_FIELDS
from ctviz.schemas.retrieval import FetchResult

logger = logging.getLogger(__name__)

type Clock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]

API_MAX_PAGE_SIZE = 1000


class TokenBucket:
    """Client-side pacing: at most one request per `interval_seconds`.

    The bucket holds one token, so idle time never builds up a burst; the API starts
    returning 429 after about 10 back-to-back requests.
    """

    def __init__(self, interval_seconds: float, clock: Clock, sleep: Sleep) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._next_allowed_at: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request may be sent, then reserve the slot."""
        async with self._lock:
            now = self._clock()
            if self._next_allowed_at is not None and now < self._next_allowed_at:
                await self._sleep(self._next_allowed_at - now)
                now = self._clock()
            self._next_allowed_at = now + self._interval_seconds


class _CacheGap(Exception):
    """A later page missed the cache after page 1 hit it; the fetch must restart live."""


class CtGovClient:
    """The only component that talks to ClinicalTrials.gov.

    `clock`, `sleep` and `random` are injectable so tests can verify pacing and backoff
    without waiting.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        cache: ResponseCache,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._limiter = TokenBucket(settings.min_request_interval_seconds, clock, sleep)
        self._http = httpx.AsyncClient(
            base_url=settings.ctgov_base_url, timeout=settings.request_timeout_seconds
        )
        self._data_timestamp: tuple[float, datetime] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    @property
    def page_size(self) -> int:
        """Page size actually sent: never above the API maximum or the record cap."""
        return min(self._settings.page_size, API_MAX_PAGE_SIZE, self._settings.max_records)

    async def fetch_all(self, params: dict[str, str]) -> FetchResult:
        """Fetch every page of a /studies query, up to `max_records`.

        Always projects with STUDY_FIELDS and sends identical params on every page
        (only pageToken changes). Studies are deduplicated by NCT ID. The result is
        flagged, not rejected, when the count does not reconcile with totalCount.
        """
        frozen = {
            **params,
            "fields": "|".join(STUDY_FIELDS),
            "countTotal": "true",
            "pageSize": str(self.page_size),
        }
        try:
            return await self._fetch_pages(frozen, read_cache=True)
        except _CacheGap:
            logger.info("Cache held only part of a paginated result; refetching live")
            return await self._fetch_pages(frozen, read_cache=False)

    async def data_timestamp(self) -> datetime:
        """The API's dataTimestamp from /version, reused for `version_ttl_seconds`."""
        now = self._clock()
        if self._data_timestamp is not None:
            fetched_at, timestamp = self._data_timestamp
            if now - fetched_at <= self._settings.version_ttl_seconds:
                return timestamp
        payload = await self._request("/version", {})
        try:
            timestamp = datetime.fromisoformat(payload["dataTimestamp"])
        except (KeyError, TypeError, ValueError) as error:
            raise UpstreamError(f"/version returned no valid dataTimestamp: {payload!r}") from error
        self._data_timestamp = (now, timestamp)
        return timestamp

    async def _fetch_pages(self, frozen: dict[str, str], *, read_cache: bool) -> FetchResult:
        """Walk nextPageToken until the last page or the record cap."""
        max_records = self._settings.max_records
        studies: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicates = 0
        total_count: int | None = None
        cached_at: datetime | None = None
        token: str | None = None
        pages = 0

        while True:
            page_params = frozen if token is None else {**frozen, "pageToken": token}
            page, stored_at = await self._get_page(
                page_params, read_cache=read_cache, cache_only=cached_at is not None
            )
            if pages == 0:
                cached_at = stored_at
                total_count = first_page_total(page)
            pages += 1

            for study in page.get("studies") or []:
                nct_id = study_nct_id(study)
                if nct_id is not None and nct_id in seen:
                    duplicates += 1
                    continue
                if nct_id is not None:
                    seen.add(nct_id)
                studies.append(study)

            token = page.get("nextPageToken")
            if len(studies) >= max_records or not token:
                break

        assert total_count is not None
        hit_cap = len(studies) >= max_records
        studies = studies[:max_records]
        truncated = hit_cap and total_count > len(studies)
        return FetchResult(
            studies=studies,
            total_count=total_count,
            truncated=truncated,
            coverage=len(studies) / total_count if total_count else None,
            reconciled=truncated or len(studies) == total_count,
            duplicates_dropped=duplicates,
            pages=pages,
            cached_at=cached_at,
        )

    async def _get_page(
        self, params: dict[str, str], *, read_cache: bool, cache_only: bool
    ) -> tuple[dict[str, Any], datetime | None]:
        """One /studies page and, if it came from the cache, when it was stored.

        `cache_only` is set once page 1 came from the cache: a later page must come from
        the same cached fetch, because its token may be stale for a live request.
        """
        key = cache_key(f"{self._settings.ctgov_base_url}/studies", params)
        if read_cache:
            hit = self._cache.get(key)
            if hit is not None:
                return hit.payload, hit.stored_at
        if cache_only:
            raise _CacheGap
        payload = await self._request("/studies", params)
        self._cache.set(key, payload)
        return payload, None

    async def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Paced GET returning JSON; retries 429, 5xx and network errors with backoff.

        Other 4xx responses fail immediately with the API's error text.
        """
        max_retries = self._settings.max_retries
        for attempt in range(max_retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._http.get(path, params=params)
            except httpx.TransportError as error:
                failure = f"network error: {error!r}"
                if attempt == max_retries:
                    raise UpstreamError(f"{path}: {failure} after {max_retries} retries") from error
            else:
                if response.status_code == 200:
                    return parse_json(response)
                failure = f"HTTP {response.status_code}"
                if response.status_code == 429:
                    if attempt == max_retries:
                        raise RateLimitError(
                            f"{path}: still rate-limited (HTTP 429) after {max_retries} retries"
                        )
                elif response.status_code >= 500:
                    if attempt == max_retries:
                        raise UpstreamError(
                            f"{path}: HTTP {response.status_code} after {max_retries} retries: "
                            f"{response.text[:300]}",
                            status_code=response.status_code,
                        )
                else:
                    raise ApiRequestError(response.status_code, response.text, str(response.url))
            delay = self._backoff_seconds(attempt)
            logger.warning("%s on %s; retry %d in %.2fs", failure, path, attempt + 1, delay)
            await self._sleep(delay)
        raise AssertionError("unreachable: the retry loop always returns or raises")

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with additive jitter: base * 2^attempt + U(0, base)."""
        base = self._settings.backoff_base_seconds
        return base * 2**attempt + self._random() * base


def first_page_total(page: dict[str, Any]) -> int:
    """totalCount from the first page; without it the fetch cannot be reconciled."""
    total = page.get("totalCount")
    if not isinstance(total, int):
        raise UpstreamError(f"first /studies page has no integer totalCount (got {total!r})")
    return total


def study_nct_id(study: dict[str, Any]) -> str | None:
    """The NCT ID used for deduplication; None if absent (normalize rejects those)."""
    return study.get("protocolSection", {}).get("identificationModule", {}).get("nctId")


def parse_json(response: httpx.Response) -> dict[str, Any]:
    """Decode a 200 body; a non-JSON or non-object body is an upstream failure."""
    try:
        payload = response.json()
    except ValueError as error:
        raise UpstreamError(
            f"{response.url}: HTTP 200 with a non-JSON body: {response.text[:300]}"
        ) from error
    if not isinstance(payload, dict):
        raise UpstreamError(f"{response.url}: expected a JSON object, got {type(payload).__name__}")
    return payload
