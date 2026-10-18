"""Choose how one cohort's counts are obtained: fetch records, or count per bucket.

Route 1 (fetch) reads every matching record and counts locally; it is exact up to the
record cap and truncated beyond it, and the first pages in API order are not a random
sample. Route 2 (count_per_bucket) applies when a cohort matches more trials than the
cap and the dimension is API-countable (`DimensionSpec.api_count`): the API counts each
bucket exactly with one countTotal request, and the bucket counts must add up to the
cohort's totalCount. When they do not, or the route would need too many requests, the
cohort is fetched instead and the reason is kept for meta.

Like the pipeline, this module orchestrates: network I/O happens in the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ctviz.aggregate.registry import DIMENSIONS, ApiCount, DimensionSpec, bucket_filters
from ctviz.config import CountRoutePolicy
from ctviz.errors import SourceRecordError
from ctviz.normalize import normalize_study
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.counting import boundary_studies, count_per_bucket
from ctviz.schemas.plan import Dimension
from ctviz.schemas.retrieval import CountResult, FetchResult


@dataclass
class CountAttempt:
    """What the count-per-bucket route did for one cohort, kept for meta.

    `requests` counts every request the route made (cohort total, date range, buckets).
    `remainder` is totalCount minus the summed bucket counts (None if no bucket was
    counted). `fallback_reason` says why the cohort was fetched instead; None when the
    counts were used.
    """

    requests: int
    remainder: int | None = None
    fallback_reason: str | None = None


@dataclass
class CohortRetrieval:
    """One cohort's retrieval: fetched records, or API-counted buckets with their samples."""

    total_count: int
    fetch: FetchResult | None = None
    count: CountResult | None = None
    attempt: CountAttempt | None = None


def count_threshold(policy: CountRoutePolicy, client: CtGovClient) -> int:
    """Cohorts matching more trials than this are counted per bucket."""
    return policy.above_total if policy.above_total is not None else client.max_records


async def retrieve_cohort(
    params: dict[str, str],
    dimension: Dimension | None,
    client: CtGovClient,
    policy: CountRoutePolicy,
) -> CohortRetrieval:
    """Retrieve one cohort by the route its size and dimension allow.

    A dimension without `api_count` (and any relation) is always fetched. Otherwise the
    fetch's first page decides, so the choice costs no extra request: at or below the
    threshold the fetch simply continues (exact, no truncation); above it the fetch stops
    after page 1 and each bucket is counted.
    """
    spec = DIMENSIONS[dimension] if dimension is not None else None
    if spec is None or spec.api_count is None:
        return await fetched(client, params)
    first = await client.fetch_unless_above(params, count_threshold(policy, client))
    if isinstance(first, FetchResult):
        return CohortRetrieval(total_count=first.total_count, fetch=first)
    return await counted(client, params, spec, spec.api_count, first.total_count, policy)


async def counted(
    client: CtGovClient,
    params: dict[str, str],
    spec: DimensionSpec,
    api_count: ApiCount,
    total: int,
    policy: CountRoutePolicy,
) -> CohortRetrieval:
    """Count each bucket; fall back to fetching when the counts cannot be trusted.

    `requests` starts at 1 for the fetch's first page, which gave the cohort total.
    """
    requests = 1
    year_range: tuple[int, int] | None = None
    if api_count.year_range_field is not None:
        first, last = await boundary_studies(client, params, api_count.year_range_field)
        requests += 2
        try:
            year_range = start_year_range(first, last)
        except ValueError as error:
            return await fallback(client, params, total, CountAttempt(requests), str(error))

    filters = bucket_filters(api_count, year_range=year_range)
    if len(filters) > policy.max_bucket_requests:
        reason = (
            f"counting each {spec.label} bucket would take {len(filters)} requests, over "
            f"MAX_COUNT_REQUESTS ({policy.max_bucket_requests})"
        )
        return await fallback(client, params, total, CountAttempt(requests), reason)

    count = await count_per_bucket(client, params, filters, policy.citation_sample_size)
    requests += len(filters)
    remainder = total - count.counted_total
    attempt = CountAttempt(requests, remainder)
    if remainder != 0:
        reason = (
            f"the {len(filters)} {spec.label} bucket counts sum to {count.counted_total:,} of "
            f"{total:,} matches (remainder {remainder:+,}), so some trials fall in no counted "
            "bucket or in several: a value the bucket filters do not cover, or data that "
            "changed between requests"
        )
        return await fallback(client, params, total, attempt, reason)
    return CohortRetrieval(total_count=total, count=count, attempt=attempt)


async def fetched(client: CtGovClient, params: dict[str, str]) -> CohortRetrieval:
    """Route 1: fetch every record up to the cap."""
    fetch = await client.fetch_all(params)
    return CohortRetrieval(total_count=fetch.total_count, fetch=fetch)


async def fallback(
    client: CtGovClient,
    params: dict[str, str],
    total: int,
    attempt: CountAttempt,
    reason: str,
) -> CohortRetrieval:
    """Fetch the cohort after the count route was rejected, keeping why for meta."""
    attempt.fallback_reason = reason
    fetch = await client.fetch_all(params)
    return CohortRetrieval(total_count=fetch.total_count, fetch=fetch, attempt=attempt)


def start_year_range(first: Any | None, last: Any | None) -> tuple[int, int] | None:
    """(first, last) start year from the cohort's earliest and latest-starting studies.

    None when the earliest study has no start date: missing dates sort last, so then no
    matching study has one. Raises ValueError when a boundary date cannot be read, since
    the years to count would then be unknown.
    """
    if first is None or last is None:
        return None
    try:
        earliest, latest = normalize_study(first), normalize_study(last)
    except SourceRecordError as error:
        raise ValueError(
            f"the earliest or latest start date could not be read ({error})"
        ) from error
    if earliest.start_date_raw is None:
        return None
    if earliest.start_date is None or latest.start_date is None:
        raise ValueError(
            "the earliest or latest start date could not be read "
            f"({earliest.start_date_raw!r}, {latest.start_date_raw!r})"
        )
    return earliest.start_date.year, latest.start_date.year
