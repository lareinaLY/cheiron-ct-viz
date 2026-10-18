"""Retrieval results passed from `retrieval/` to the pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FetchResult(BaseModel):
    """Every study of one /studies query, plus the numbers needed to reconcile it.

    `studies` are raw API dicts, deduplicated by NCT ID. `reconciled` is False when the
    fetch ran to the end but the number of studies differs from `total_count`
    (invariant 6); the pipeline reports that in meta instead of failing.
    """

    studies: list[dict[str, Any]]
    total_count: int = Field(description="totalCount from the first page.")
    truncated: bool = Field(description="True when the record cap stopped the fetch early.")
    coverage: float | None = Field(description="len(studies) / total_count; None if 0 matched.")
    reconciled: bool
    duplicates_dropped: int = Field(default=0, description="Repeated NCT IDs across pages.")
    pages: int
    cached_at: datetime | None = Field(
        default=None, description="When the cached pages were stored; None if fetched live."
    )
