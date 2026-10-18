"""Retrieval results passed from `retrieval/` to the pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FetchResult(BaseModel):
    """Every study of one /studies query, plus the numbers needed to reconcile it.

    `studies` are the raw decoded JSON values of `studies[]`, deduplicated by NCT ID and
    deliberately not validated here: a malformed study must reach normalize, which skips
    and reports it, instead of failing the whole fetch. `reconciled` is False when the
    fetch ran to the end but the number of studies differs from `total_count`
    (invariant 6); the pipeline reports that in meta instead of failing.
    """

    studies: list[Any]
    total_count: int = Field(description="totalCount from the first page.")
    truncated: bool = Field(description="True when the record cap stopped the fetch early.")
    coverage: float | None = Field(description="len(studies) / total_count; None if 0 matched.")
    reconciled: bool
    duplicates_dropped: int = Field(default=0, description="Repeated NCT IDs across pages.")
    pages: int
    cached_at: datetime | None = Field(
        default=None, description="When the cached pages were stored; None if fetched live."
    )


class StudySample(BaseModel):
    """The first page of a /studies query: its totalCount and up to `pageSize` studies.

    Used by the count-per-bucket route, where totalCount is the answer and the studies
    are only citations. `studies` are raw decoded JSON, as in FetchResult.
    """

    total_count: int
    studies: list[Any]
    cached_at: datetime | None = None


class BucketCount(BaseModel):
    """The API's exact count for one bucket filter, plus the sample studies it returned."""

    key: str
    filter_clause: str = Field(description="The bucket's filter.advanced clause.")
    total_count: int
    studies: list[Any]
    cached_at: datetime | None = Field(
        default=None, description="When the cached response was stored; None if fetched live."
    )


class CountResult(BaseModel):
    """Every bucket of one cohort counted by the API (the count-per-bucket route)."""

    buckets: list[BucketCount]

    @property
    def counted_total(self) -> int:
        """Sum of the bucket counts; equals the cohort's totalCount when they reconcile."""
        return sum(bucket.total_count for bucket in self.buckets)


class EnumDrift(BaseModel):
    """Differences between GET /studies/enums and the enums this code knows.

    `new_values` maps an API enum type (e.g. "Phase") to the values the API lists that we
    do not; `missing_types` are enum types we compare that the API no longer lists.
    """

    new_values: dict[str, list[str]] = Field(default_factory=dict)
    missing_types: list[str] = Field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        """True when the API's vocabulary has values or types we do not handle."""
        return bool(self.new_values or self.missing_types)
