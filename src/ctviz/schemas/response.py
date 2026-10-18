"""VisualizationResponse: the HTTP API's output, a chart spec plus full provenance."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import ChartType, Cohort, MatchMode, QueryPlan


class CountUnit(StrEnum):
    """What one unit of `count` means."""

    TRIALS = "trials"


class NodeType(StrEnum):
    """Kind of entity a network node represents."""

    SPONSOR = "sponsor"
    DRUG = "drug"
    CONDITION = "condition"


class FieldType(StrEnum):
    """Vega-Lite measurement type of an encoded field."""

    NOMINAL = "nominal"
    TEMPORAL = "temporal"
    QUANTITATIVE = "quantitative"


class Datum(BaseModel):
    """One bar or point: a dimension value and the number of trials in it."""

    key: str = Field(description="Dimension value as grouped, e.g. 'Phase 1/Phase 2' or '2024'.")
    label: str
    cohort: str | None = None
    count: int
    estimated_count: int = Field(
        default=0, description="Trials counted here via an estimated date."
    )
    unknown_date_type_count: int = Field(
        default=0,
        description="Trials counted here via a date with no ACTUAL/ESTIMATED type (legacy).",
    )
    incomplete: bool = Field(
        default=False, description="Period still in progress (e.g. this year)."
    )
    is_unknown: bool = Field(default=False, description="Bucket of trials missing this value.")
    citations: list[Citation] = Field(default_factory=list)
    citation_count_total: int = Field(description="True number of citations before the cap.")


class NetworkNode(BaseModel):
    """A network node; `size` is the number of trials the entity appears in."""

    id: str
    label: str
    type: NodeType
    size: int
    citations: list[Citation] = Field(default_factory=list)
    citation_count_total: int


class NetworkEdge(BaseModel):
    """An undirected edge; `weight` is the number of trials where both endpoints co-occur."""

    source: str
    target: str
    weight: int
    citations: list[Citation] = Field(default_factory=list)
    citation_count_total: int


class NodeElement(BaseModel):
    """A Cytoscape-style node element: the node itself sits under `data`."""

    data: NetworkNode


class EdgeData(NetworkEdge):
    """Edge payload with the `id` Cytoscape requires for every element."""

    id: str


class EdgeElement(BaseModel):
    """A Cytoscape-style edge element: the edge itself sits under `data`."""

    data: EdgeData


class Channel(BaseModel):
    """One Vega-Lite-style encoding channel.

    `sort` lists the domain values in display order when the order is not alphabetical
    (e.g. phases, enrollment bins, counts descending).
    """

    field: str
    type: FieldType
    title: str | None = None
    sort: list[str] | None = None
    time_unit: Literal["year", "yearmonth"] | None = None


class Encoding(BaseModel):
    """Which datum (or node) fields map to which visual channels."""

    x: Channel | None = None
    y: Channel | None = None
    color: Channel | None = None
    size: Channel | None = None


class Visualization(BaseModel):
    """The chart spec. Network charts use `nodes`/`edges`; all others use `data`."""

    type: ChartType
    title: str
    encoding: Encoding = Field(default_factory=Encoding)
    data: list[Datum] = Field(default_factory=list)
    nodes: list[NodeElement] = Field(default_factory=list)
    edges: list[EdgeElement] = Field(default_factory=list)


class Interpretation(BaseModel):
    """The question as asked and the plan that was executed for it.

    `planned_by` says where the plan came from. With "structured_fields" the question
    text was NOT interpreted: the request's structured fields alone define the query.
    """

    question: str
    plan: QueryPlan
    planned_by: Literal["structured_fields"]


class CohortStats(BaseModel):
    """Retrieval accounting for one cohort (invariant 6: counts reconcile with totalCount)."""

    label: str
    total_matched: int = Field(description="totalCount reported by the API.")
    records_retrieved: int
    records_skipped: int = Field(description="Retrieved records the normalizer rejected.")
    truncated: bool = Field(description="True when the record cap stopped the fetch early.")
    coverage: float | None = Field(description="records_retrieved / total_matched.")
    reconciled: bool = Field(
        description="records_retrieved equals total_matched, or the fetch was truncated."
    )


class TopNSummary(BaseModel):
    """What a `top_n` limit removed from the chart.

    The Unknown bucket is never removed. `trials_omitted` counts, per cohort, the trials
    that appear only in omitted buckets (so they are not visible anywhere in the chart).
    """

    top_n: int
    omitted_keys: list[str]
    trials_omitted: dict[str, int] = Field(description="Cohort label -> omitted trials.")


class Meta(BaseModel):
    """Everything a reader needs to trust or question the chart (invariant 3: nothing is silent)."""

    interpretation: Interpretation
    count_unit: CountUnit
    filters_applied: list[Cohort]
    match_mode: MatchMode
    search_semantics: dict[str, str] = Field(
        default_factory=dict,
        description="Filter name -> what the API actually matches for that filter.",
    )
    phase_bucketing: Literal["exclusive_combined"] = "exclusive_combined"
    country_granularity: Literal["country_as_reported"] = "country_as_reported"
    buckets_overlap: bool
    total_matched: int | None = Field(description="totalCount reported by the API.")
    records_retrieved: int
    truncated: bool
    coverage: float | None = Field(description="records_retrieved / total_matched.")
    cohorts: list[CohortStats] = Field(default_factory=list)
    top_n: TopNSummary | None = None
    estimated_count: int | None = Field(
        description="Trials counted via an estimated value, summed over cohorts; None for networks."
    )
    unknown_date_type_count: int | None = Field(
        description="Trials placed by a date with no ACTUAL/ESTIMATED type, summed over "
        "cohorts; None unless the dimension is temporal."
    )
    data_timestamp: datetime | None = Field(description="API dataTimestamp from /version.")
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timing_ms: dict[str, float] = Field(default_factory=dict)


class VisualizationResponse(BaseModel):
    """Top-level response body."""

    visualization: Visualization
    meta: Meta


class ErrorResponse(BaseModel):
    """Structured error body returned instead of a chart (invariant 7)."""

    code: str = Field(description="Machine-readable error code.")
    message: str
    suggestions: list[str] = Field(default_factory=list)
