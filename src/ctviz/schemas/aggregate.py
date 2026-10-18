"""Aggregation results passed from `aggregate/` to `spec/`."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import NetworkEdge, NetworkNode, NetworkSummary, TopNSummary


class Bucket(BaseModel):
    """Trials sharing one value of a dimension.

    `key` is the grouping identity (e.g. a MeSH id); `label` is the display text
    (e.g. the MeSH term). They are equal for most dimensions.
    """

    key: str
    label: str
    count: int
    estimated_count: int = Field(default=0, description="Trials here whose date is ESTIMATED.")
    unknown_date_type_count: int = Field(
        default=0, description="Trials here whose date has no ACTUAL/ESTIMATED type."
    )
    incomplete: bool = Field(default=False, description="Period still in progress at as_of.")
    is_future: bool = Field(default=False, description="Period starts after as_of (projection).")
    is_unknown: bool = Field(default=False, description="Trials missing this value.")
    citations: list[Citation] = Field(default_factory=list)
    citation_count_total: int = Field(description="True number of citations before the cap.")


class GroupResult(BaseModel):
    """Output of `aggregate.group_by`.

    `buckets_overlap` is True when one trial can count in several buckets
    (e.g. a multi-country trial), so the bucket counts can add up to more than the
    number of trials. `assumptions` and `warnings` are copied into the response meta.
    """

    dimension: Dimension
    buckets: list[Bucket]
    buckets_overlap: bool
    estimated_count: int = Field(default=0, description="Trials counted via an estimated value.")
    unknown_date_type_count: int | None = Field(
        default=None,
        description="Temporal dimensions only: trials placed by a date with no "
        "ACTUAL/ESTIMATED type. None when the dimension is not temporal.",
    )
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Graph(BaseModel):
    """Output of `aggregate.build_network`.

    `assumptions` and `warnings` are copied into the response meta by the spec stage, and
    `summary` (set by `build_network`) becomes `meta.network`.
    """

    relation: Relation
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]
    summary: NetworkSummary | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CohortGroup(BaseModel):
    """One cohort's grouping inside a `GroupedCohorts` result."""

    label: str
    result: GroupResult


class GroupedCohorts(BaseModel):
    """Output of `aggregate.group_cohorts`: one GroupResult per cohort on a shared axis.

    `key_order` is the display order of every bucket key across all cohorts (the
    dimension's own order, Unknown last), so cohorts line up on one axis. `top_n` is set
    when a bucket limit was requested; it lists what was removed.
    """

    dimension: Dimension
    cohorts: list[CohortGroup] = Field(min_length=1)
    key_order: list[str]
    top_n: TopNSummary | None = None
