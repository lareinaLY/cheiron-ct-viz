"""QueryPlan: the only thing the LLM produces.

It describes what to fetch and how to group it. It never contains data values.
Cross-field rules (dimension vs relation, chart compatibility) are enforced by
`planner.validate_plan`, not here, so that a plan the LLM emits can always be parsed
and then rejected with a specific reason.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ctviz.schemas.trial import OverallStatus, Phase


class Dimension(StrEnum):
    """A per-trial attribute to group trials by."""

    PHASE = "phase"
    START_YEAR = "start_year"
    START_MONTH = "start_month"
    COUNTRY = "country"
    SPONSOR_CLASS = "sponsor_class"
    LEAD_SPONSOR = "lead_sponsor"
    INTERVENTION_TYPE = "intervention_type"
    OVERALL_STATUS = "overall_status"
    CONDITION = "condition"
    INTERVENTION_MESH = "intervention_mesh"
    ENROLLMENT_BUCKET = "enrollment_bucket"


class Relation(StrEnum):
    """A pairwise relation rendered as a network."""

    SPONSOR_DRUG = "sponsor_drug"
    DRUG_DRUG_COOCCURRENCE = "drug_drug_cooccurrence"
    CONDITION_DRUG = "condition_drug"


class ChartType(StrEnum):
    """Visualization types the spec stage can emit."""

    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    TIME_SERIES = "time_series"
    NETWORK_GRAPH = "network_graph"
    HISTOGRAM = "histogram"


MatchMode = Literal["intervention_name", "broad"]


class Filters(BaseModel):
    """Retrieval filters for one cohort. All are optional and combined with AND."""

    model_config = ConfigDict(extra="forbid")

    drug: str | None = None
    condition: str | None = None
    sponsor: str | None = None
    country: str | None = None
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(default_factory=list)
    start_year_from: int | None = None
    start_year_to: int | None = None


class Cohort(BaseModel):
    """A labelled set of filters. Two or more cohorts make a comparison."""

    model_config = ConfigDict(extra="forbid")

    label: str
    filters: Filters


class QueryPlan(BaseModel):
    """Structured translation of a user question.

    Exactly one of `dimension` or `relation` must be set; `planner.validate_plan`
    enforces this.
    """

    model_config = ConfigDict(extra="forbid")

    cohorts: list[Cohort] = Field(min_length=1)
    dimension: Dimension | None = None
    relation: Relation | None = None
    metric: Literal["trial_count"] = "trial_count"
    chart_hint: ChartType | None = None
    time_granularity: Literal["year", "month"] | None = None
    top_n: int | None = None
    match_mode: MatchMode = "intervention_name"
