"""VisualizationResponse: the HTTP API's output, a chart spec plus full provenance."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, JsonValue

from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import ChartType, Cohort, LlmPlanDraft, MatchMode, QueryPlan


class CountUnit(StrEnum):
    """What one unit of `count` means."""

    TRIALS = "trials"


class DataQuality(StrEnum):
    """How much of the retrieved data could be read.

    - normal: few or no retrieved records were skipped as unreadable.
    - degraded: enough records were skipped (DEGRADED_SKIP_RATIO, MIN_SKIPPED_FOR_DEGRADED)
      that counts and rankings may be off; a prominent warning says by how much.
    """

    NORMAL = "normal"
    DEGRADED = "degraded"


class ResponseMode(StrEnum):
    """How an LLM provider is asked for structured output.

    - json_schema_strict: the provider enforces the JSON schema while decoding.
    - json_object: the provider only guarantees valid JSON; the schema is embedded in
      the system prompt and the answer is validated afterwards.
    """

    JSON_SCHEMA_STRICT = "json_schema_strict"
    JSON_OBJECT = "json_object"


class NodeType(StrEnum):
    """Kind of entity a network node represents."""

    SPONSOR = "sponsor"
    DRUG = "drug"
    CONDITION = "condition"


class ExclusionReason(StrEnum):
    """Why an intervention MeSH term is not drawn as a network node.

    - comparator: no active treatment (standard of care, observation, placebo vehicle).
    - mesh_mismapping: the derived MeSH term is not an intervention at all (e.g. "Taxes"
      derived from "taxane").
    """

    COMPARATOR = "comparator"
    MESH_MISMAPPING = "mesh_mismapping"


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
    is_future: bool = Field(
        default=False,
        description="Period starts after the as-of date, so its trials have planned "
        "(estimated) start dates: a projection, not activity.",
    )
    is_unknown: bool = Field(default=False, description="Bucket of trials missing this value.")
    is_unrecognized: bool = Field(
        default=False,
        description="Bucket of trials whose value is outside the known API enum (the API "
        "may have added a value); distinct from is_unknown.",
    )
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


class ExcludedTerm(BaseModel):
    """A deny-listed MeSH term that was left out of a network, and how many trials listed it."""

    id: str = Field(description="MeSH id, e.g. 'D059039'.")
    label: str = Field(description="MeSH term as observed, e.g. 'Standard of Care'.")
    reason: ExclusionReason
    trials: int = Field(description="Trials in the result set that list this term.")


class NetworkSummary(BaseModel):
    """What a network response leaves out, and why (invariant 3: nothing is silent).

    Counts with `_total` describe the graph after deny-list exclusion and before the
    readability caps; `_shown` describe what the response contains. A trial "without a
    shown edge" contributed at least one edge before trimming and none after; a trial
    "without a shown node" appeared in at least one node before trimming and in none after.
    """

    excluded_terms: list[ExcludedTerm] = Field(default_factory=list)
    trials_with_excluded_terms: int = 0
    max_nodes: int | None = Field(description="Node cap; None means no cap.")
    min_edge_weight: int = Field(description="Edges with fewer co-occurring trials are omitted.")
    nodes_total: int
    nodes_shown: int
    edges_total: int
    edges_shown: int
    edges_below_min_weight: int
    edges_with_omitted_node: int = Field(
        description="Edges at or above the weight threshold dropped because an endpoint "
        "was omitted by the node cap."
    )
    trials_with_edges: int
    trials_without_shown_edge: int
    trials_without_shown_node: int
    edge_citation_cap: int = Field(
        description="Citations kept per edge (citation_count_total keeps the true number)."
    )
    edge_citations_reduced: bool = Field(
        default=False,
        description="True when edge citations were cut to fit the response size budget.",
    )
    response_bytes_before_reduction: int | None = None


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


class FieldOverride(BaseModel):
    """An explicit request field that replaced what the LLM read from the question.

    `path` names the plan field, e.g. "cohorts[1].filters.drug" or "dimension". When an
    explicit dimension replaces the LLM's relation (or the reverse), the cleared field is
    recorded with `explicit_value` None.
    """

    path: str
    llm_value: JsonValue
    explicit_value: JsonValue


PlannedBy = Literal["llm", "rules", "structured_fields"]


class Interpretation(BaseModel):
    """The question as asked, the plan that was executed for it, and where the plan came from.

    `planned_by`:
    - "llm": the LLM translated the question into `draft`, explicit request fields were
      applied on top (`overrides`), and the result was validated.
    - "rules": no LLM is configured; the request's structured fields alone define the
      query and the question text was NOT interpreted.
    - "structured_fields": a QueryPlan was passed to the pipeline directly by code (no
      planner ran); the question text was not interpreted.
    `rejected_attempts` holds the validation error of each LLM draft that was rejected
    and retried. `model` is the primary model of the LLM fallback chain; `provider`,
    `model_used` and `response_mode` describe the call that produced the accepted draft,
    and `fallback_reason` says why earlier providers in the chain were passed over for
    that call (None when the first provider answered).
    """

    question: str
    plan: QueryPlan
    planned_by: PlannedBy
    model: str | None = Field(default=None, description="Primary model of the LLM chain.")
    provider: str | None = Field(default=None, description="LLM provider that drafted the plan.")
    model_used: str | None = Field(default=None, description="LLM model that drafted the plan.")
    response_mode: ResponseMode | None = Field(
        default=None, description="How that provider was asked for structured output."
    )
    fallback_reason: str | None = Field(
        default=None,
        description="Why earlier providers in the chain did not answer; None when the "
        "primary provider answered.",
    )
    draft: LlmPlanDraft | None = Field(default=None, description="The accepted LLM draft.")
    overrides: list[FieldOverride] = Field(default_factory=list)
    rejected_attempts: list[str] = Field(default_factory=list)
    ranking_top_n: int | None = Field(
        default=None,
        description="top_n chosen by code (the configured default) because the question "
        "asked for a ranking without giving a number; None otherwise.",
    )
    planning_ms: float | None = Field(default=None, description="Planner wall-clock time.")


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
    data_quality: DataQuality = Field(
        default=DataQuality.NORMAL,
        description="'degraded' when many retrieved records were unreadable and skipped.",
    )
    top_n: TopNSummary | None = None
    network: NetworkSummary | None = Field(
        default=None, description="Exclusions and readability caps; networks only."
    )
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
