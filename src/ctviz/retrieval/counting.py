"""Requests of the count-per-bucket route: the API counts each bucket exactly.

For a dimension whose buckets can be written as `filter.advanced` clauses, one
countTotal request per bucket gives its exact count, however many trials match, where a
fetch stops at the record cap. Each request also returns a small sample of the bucket's
studies, which become its citations.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.query_builder import with_bucket_filter
from ctviz.schemas.retrieval import BucketCount, CountResult


async def count_per_bucket(
    client: CtGovClient,
    params: dict[str, str],
    bucket_filters: Sequence[tuple[str, str]],
    sample_size: int,
) -> CountResult:
    """Count every (key, clause) bucket within the cohort's params, one request each.

    Requests are sequential and go through the client's rate limiter.
    """
    buckets: list[BucketCount] = []
    for key, clause in bucket_filters:
        sample = await client.sample(with_bucket_filter(params, clause), sample_size)
        buckets.append(
            BucketCount(
                key=key,
                filter_clause=clause,
                total_count=sample.total_count,
                studies=sample.studies,
                cached_at=sample.cached_at,
            )
        )
    return CountResult(buckets=buckets)


async def boundary_studies(
    client: CtGovClient, params: dict[str, str], sort_field: str
) -> tuple[Any | None, Any | None]:
    """The cohort's study with the lowest and with the highest `sort_field` value.

    The API sorts studies missing the field last in both directions, so a returned study
    lacks the field only when no matching study has it. None when nothing matches.
    """
    first = await client.sample({**params, "sort": f"{sort_field}:asc"}, 1)
    last = await client.sample({**params, "sort": f"{sort_field}:desc"}, 1)
    return (
        first.studies[0] if first.studies else None,
        last.studies[0] if last.studies else None,
    )
