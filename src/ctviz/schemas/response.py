"""VisualizationResponse: the HTTP API's output, a chart spec plus full provenance."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import ChartType, Cohort, QueryPlan


class CountUnit(StrEnum):
    """What one unit of `count` means."""

    TRIALS = "trials"


class NodeType(StrEnum):
    """Kind of entity a network node represents."""

    SPONSOR = "sponsor"
    DRUG = "drug"
    CONDITION = "condition"


class Datum(BaseModel):
    """One bar or point: a dimension value and the number of trials in it."""

    key: str = Field(description="Dimension value as grouped, e.g. 'Phase 1/Phase 2' or '2024'.")
    label: str
    cohort: str | None = None
    count: int
    estimated_count: int = Field(
        default=0, description="Trials counted here via an estimated date."
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


class Encoding(BaseModel):
    """Which datum fields map to which visual channels."""

    x: str | None = None
    y: str | None = None
    color: str | None = None


class Visualization(BaseModel):
    """The chart spec. Network charts use `nodes`/`edges`; all others use `data`."""

    type: ChartType
    title: str
    encoding: Encoding = Field(default_factory=Encoding)
    data: list[Datum] = Field(default_factory=list)
    nodes: list[NetworkNode] = Field(default_factory=list)
    edges: list[NetworkEdge] = Field(default_factory=list)


class Meta(BaseModel):
    """Everything a reader needs to trust or question the chart (invariant 3: nothing is silent)."""

    interpretation: QueryPlan = Field(description="Echo of the validated plan.")
    count_unit: CountUnit
    filters_applied: list[Cohort]
    match_mode: Literal["intervention_name", "broad"]
    phase_bucketing: Literal["exclusive_combined"] = "exclusive_combined"
    country_granularity: Literal["country_as_reported"] = "country_as_reported"
    buckets_overlap: bool
    total_matched: int | None = Field(description="totalCount reported by the API.")
    records_retrieved: int
    truncated: bool
    coverage: float | None = Field(description="records_retrieved / total_matched.")
    data_timestamp: datetime | None = Field(description="API dataTimestamp from /version.")
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timing_ms: dict[str, float] = Field(default_factory=dict)


class VisualizationResponse(BaseModel):
    """Top-level response body."""

    visualization: Visualization
    meta: Meta
