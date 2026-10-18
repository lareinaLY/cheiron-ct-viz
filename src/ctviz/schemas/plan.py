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

# Bounds on numeric plan values. A dimension has at most a few hundred buckets, so a
# larger top_n is a typo; years outside this range are typos too, and a year that is not
# four digits cannot be written as an AREA[StartDate] date at all.
MAX_TOP_N = 1000
MIN_START_YEAR = 1900
MAX_START_YEAR = 2100


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


class Clarification(BaseModel):
    """A question the user must answer because their question has two readings."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(description="One short question naming the ambiguity.")
    options: list[str] = Field(description="The readings to choose from, usually two.")


class LlmPlanDraft(BaseModel):
    """What the LLM returns: a QueryPlan draft, or a reason it cannot make one.

    Separate from QueryPlan so the model can decline (`unsupported_reason`) or ask
    (`clarification`) instead of guessing, and so cohort labels stay deterministic:
    the planner derives them from the filters. Every field is required and nullable,
    which is what OpenAI strict structured outputs expect.
    """

    model_config = ConfigDict(extra="forbid")

    cohorts: list[Filters] = Field(
        description="One filter set per cohort; two or more only when comparing groups."
    )
    dimension: Dimension | None = Field(description="The attribute to group trials by.")
    relation: Relation | None = Field(description="The relation to draw as a network.")
    chart_hint: ChartType | None = Field(description="Only when the user names a chart type.")
    top_n: int | None = Field(description="Only when the user asks for a number of top values.")
    ranking_intent: bool = Field(
        description="True only when the question asks for the most, top or leading values. "
        "Never a number: code chooses how many values to show."
    )
    match_mode: MatchMode = Field(
        description="intervention_name unless the user asks for any mention of the drug."
    )
    unsupported_reason: str | None = Field(
        description="Why the question cannot be answered with the available data."
    )
    clarification: Clarification | None = Field(
        description="Set when the question has two plausible readings."
    )
