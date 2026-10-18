"""QueryPlan: the only thing the LLM produces.

It describes what to fetch and how to group it. It never contains data values.
Cross-field rules (dimension vs relation, chart compatibility) are enforced by
`planner.validate_plan`, not here, so that a plan the LLM emits can always be parsed
and then rejected with a specific reason.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class RankingDirection(StrEnum):
    """Which end of a ranking the question asks for: the most trials or the fewest."""

    MOST = "most"
    FEWEST = "fewest"


MatchMode = Literal["intervention_name", "broad"]

# Bounds on numeric plan values. A dimension has at most a few hundred buckets, so a
# larger top_n is a typo; years outside this range are typos too, and a year that is not
# four digits cannot be written as an AREA[StartDate] date at all.
MAX_TOP_N = 1000
MIN_START_YEAR = 1900
MAX_START_YEAR = 2100
# Input size limits (engineering review M3): a question is a sentence or two, a filter
# value a name; anything longer is a mistake or abuse that would be billed as LLM tokens
# or sent upstream as an oversized URL.
MAX_QUERY_LENGTH = 2000
MAX_FILTER_TEXT_LENGTH = 200


def deduplicated[T](values: list[T]) -> list[T]:
    """The values in first-seen order without repeats; an OR list means the same."""
    return list(dict.fromkeys(values))


def check_text_length(value: str | None, limit: int = MAX_FILTER_TEXT_LENGTH) -> str | None:
    """Raise ValueError for a value longer than `limit` characters."""
    if value is not None and len(value) > limit:
        raise ValueError(f"must be at most {limit} characters, got {len(value)}")
    return value


def check_unique_enum_list[E: StrEnum](values: list[E], enum_type: type[E]) -> list[E]:
    """Deduplicate an enum list; raise ValueError when it is longer than the enum."""
    if len(values) > len(enum_type):
        raise ValueError(
            f"must list at most {len(enum_type)} values (one per {enum_type.__name__}), "
            f"got {len(values)}"
        )
    return deduplicated(values)


class Filters(BaseModel):
    """Retrieval filters for one cohort. All are optional and combined with AND.

    Free-text values are at most MAX_FILTER_TEXT_LENGTH characters, and the phase and
    status lists are deduplicated and at most one entry per enum value. The bounds are
    checked in validators, not as JSON-schema keywords, because this model is part of the
    LLM's strict output schema, where such keywords are not reliably supported; an LLM
    draft over a bound is rejected and redrafted like any invalid draft.
    """

    model_config = ConfigDict(extra="forbid")

    drug: str | None = None
    condition: str | None = None
    sponsor: str | None = None
    country: str | None = None
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(default_factory=list)
    start_year_from: int | None = None
    start_year_to: int | None = None

    @field_validator("drug", "condition", "sponsor", "country")
    @classmethod
    def text_within_limit(cls, value: str | None) -> str | None:
        """Bound free-text filters (MAX_FILTER_TEXT_LENGTH)."""
        return check_text_length(value)

    @field_validator("phases")
    @classmethod
    def unique_phases(cls, phases: list[Phase]) -> list[Phase]:
        """Deduplicate phases; more entries than phases exist is an error."""
        return check_unique_enum_list(phases, Phase)

    @field_validator("statuses")
    @classmethod
    def unique_statuses(cls, statuses: list[OverallStatus]) -> list[OverallStatus]:
        """Deduplicate statuses; more entries than statuses exist is an error."""
        return check_unique_enum_list(statuses, OverallStatus)


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
    bottom_n: int | None = Field(
        default=None,
        description="Keep the N known buckets with the fewest trials; exclusive with top_n.",
    )
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
    top_n: int | None = Field(
        description="Only when the user gives a number of values to show (top or bottom)."
    )
    ranking_direction: RankingDirection | None = Field(
        description="'most' when the question asks for the most, top or leading values; "
        "'fewest' only for fewest, least or lowest; null when it asks for no ranking. "
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
    unhandled_terms: list[str] = Field(
        description="Words or phrases of the question that no filter, dimension or "
        "relation expresses, copied from the question; empty when every part is mapped."
    )
