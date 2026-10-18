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
