"""Aggregation results passed from `aggregate/` to `spec/`."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import (
    CohortsOverlap,
    NetworkEdge,
    NetworkNode,
    NetworkSummary,
    Notice,
    TopNSummary,
)
from ctviz.schemas.trial import TrialRecord


class Bucket(BaseModel):
    """Trials sharing one value of a dimension.

    `key` is the grouping identity (e.g. a MeSH id); `label` is the display text
    (e.g. the MeSH term). They are equal for most dimensions.
    """

    key: str
    label: str
    count: int
    estimated_count: int | None = Field(
        default=None,
        description="Trials here whose date or enrollment is ESTIMATED; None when the "
        "dimension has no estimate concept or the count was not determined (an API-counted "
        "temporal bucket).",
    )
    unknown_date_type_count: int | None = Field(
        default=None,
        description="Trials here whose start date has no ACTUAL/ESTIMATED type; None unless "
        "the dimension is a start date, and when not determined.",
    )
    incomplete: bool = Field(default=False, description="Period still in progress at as_of.")
    is_future: bool = Field(default=False, description="Period starts after as_of (projection).")
    is_unknown: bool = Field(default=False, description="Trials missing this value.")
    is_unrecognized: bool = Field(
        default=False, description="Trials whose value is outside the known API enum."
    )
    is_not_applicable: bool = Field(
        default=False, description="Trials for which the dimension does not apply."
    )
    is_other: bool = Field(
        default=False, description="The 'Other (N values)' bucket of a top/bottom-N result."
    )
    citations: list[Citation] = Field(default_factory=list)
    cited_trials_total: int = Field(description="Distinct trials cited before the cap.")
    citation_count_total: int = Field(description="Citations before the cap.")
    rank: tuple[int, ...] = Field(
        default=(), description="Sort rank for BY_RANK dimensions (e.g. phase order)."
    )


class GroupResult(BaseModel):
    """Output of `aggregate.group_by`.

    `buckets_overlap` is True when one trial can count in several buckets
    (e.g. a multi-country trial), so the bucket counts can add up to more than the
    number of trials. `assumptions` and `warnings` are copied into the response meta.
    """

    dimension: Dimension
    buckets: list[Bucket]
    buckets_overlap: bool
    estimated_count: int | None = Field(
        default=None,
        description="Trials counted via an estimated value; None when the dimension has no "
        "estimate concept or the count was not determined.",
    )
    unknown_date_type_count: int | None = Field(
        default=None,
        description="Start-date dimensions only: trials placed by a date with no "
        "ACTUAL/ESTIMATED type. None for other dimensions and when not determined.",
    )
    assumptions: list[Notice] = Field(default_factory=list)
    warnings: list[Notice] = Field(default_factory=list)


class Graph(BaseModel):
    """Output of `aggregate.build_network`.

    `assumptions` and `warnings` are copied into the response meta by the spec stage, and
    `summary` (set by `build_network`) becomes `meta.network`.
    """

    relation: Relation
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]
    summary: NetworkSummary | None = None
    assumptions: list[Notice] = Field(default_factory=list)
    warnings: list[Notice] = Field(default_factory=list)


class CountedBucket(BaseModel):
    """One bucket counted by the API: its exact count and the sample records it returned.

    `count` is the API's totalCount for the bucket's filter; `records` are the normalized
    sample studies (the first page), used only for citations, labels and ordering.
    """

    key: str
    count: int = Field(ge=0)
    records: list[TrialRecord] = Field(default_factory=list)


class CountedCohort(BaseModel):
    """A cohort answered by the count-per-bucket route, ready for aggregation.

    `total_count` is the cohort's totalCount; the bucket counts (including the Unknown
    bucket) sum to it, which the pipeline checks before aggregating.
    """

    total_count: int = Field(ge=0)
    buckets: list[CountedBucket]


class CohortGroup(BaseModel):
    """One cohort's grouping inside a `GroupedCohorts` result."""

    label: str
    result: GroupResult


class GroupedCohorts(BaseModel):
    """Output of `aggregate.group_cohorts`: one GroupResult per cohort on a shared axis.

    `key_order` is the display order of every bucket key across all cohorts (the
    dimension's own order, Unknown last), so cohorts line up on one axis. `top_n` is set
    when a bucket limit (top or bottom N) was requested; it lists what was removed.
    `cohorts_overlap` is set when there are two or more cohorts and every one was fetched
    record by record. `assumptions` and `warnings` concern all cohorts at once (the
    ranking cutoff, the Other bucket); per-cohort ones are in each cohort's result.
    """

    dimension: Dimension
    cohorts: list[CohortGroup] = Field(min_length=1)
    key_order: list[str]
    top_n: TopNSummary | None = None
    cohorts_overlap: CohortsOverlap | None = None
    assumptions: list[Notice] = Field(default_factory=list)
    warnings: list[Notice] = Field(default_factory=list)
