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
    - partial: the record cap stopped at least one cohort's fetch, so counts cover only
      the API's first pages; the title says so. It takes precedence over degraded, whose
      warning still comes first in meta.warnings.
    """

    NORMAL = "normal"
    DEGRADED = "degraded"
    PARTIAL = "partial"


class RetrievalRoute(StrEnum):
    """How the counts were obtained from ClinicalTrials.gov.

    - fetch: every matching record was fetched (up to the record cap) and counted locally.
    - count_per_bucket: the cohort matched more trials than the record cap and the
      dimension's buckets can be expressed as API filters, so the API counted each bucket
      exactly (one countTotal request per bucket); citations are a sample of each bucket.
    - mixed: cohorts of one comparison used different routes (meta.cohorts says which).
    """

    FETCH = "fetch"
    COUNT_PER_BUCKET = "count_per_bucket"
    MIXED = "mixed"


class ResponseMode(StrEnum):
    """How an LLM provider is asked for structured output.

    - json_schema_strict: the provider enforces the JSON schema while decoding.
    - json_object: the provider only guarantees valid JSON; the schema is embedded in
      the system prompt and the answer is validated afterwards.
    """

    JSON_SCHEMA_STRICT = "json_schema_strict"
    JSON_OBJECT = "json_object"


class NodeType(StrEnum):
    """Kind of entity a network node represents.

    `drug_class` marks an intervention MeSH descriptor whose tree numbers are all in D26
    (Pharmaceutical Preparations) or D27 (Chemical Actions and Uses): a drug class or
    dosage form, not a specific drug. Its node id keeps the `drug:` prefix.
    """

    SPONSOR = "sponsor"
    DRUG = "drug"
    DRUG_CLASS = "drug_class"
    CONDITION = "condition"


class ExclusionReason(StrEnum):
    """Why an intervention MeSH term is not drawn as a drug network node.

    - comparator: a Chemicals and Drugs descriptor that is a comparator without active
      treatment (Saline Solution, the term placebo arms map to; Placebos).
    - not_in_chemicals_and_drugs_branch: a descriptor with no MeSH tree number in branch D,
      e.g. Radiotherapy (E02), Specimen Handling (E01/E05), Taxes (N03, derived from
      "taxane").
    """

    COMPARATOR = "comparator"
    NOT_IN_CHEMICALS_AND_DRUGS_BRANCH = "not_in_chemicals_and_drugs_branch"


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
    estimated_count: int | None = Field(
        default=0,
        description="Trials counted here via an estimated date; null when not determined "
        "(count_per_bucket route on a temporal dimension).",
    )
    unknown_date_type_count: int | None = Field(
        default=0,
        description="Trials counted here via a date with no ACTUAL/ESTIMATED type (legacy); "
        "null when not determined (count_per_bucket route on a temporal dimension).",
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
    """A MeSH term that was left out of a drug network, and how many trials listed it."""

    id: str = Field(description="MeSH id, e.g. 'D059039'.")
    label: str = Field(description="MeSH term as observed, e.g. 'Standard of Care'.")
    reason: ExclusionReason
    trials: int = Field(description="Trials in the result set that list this term.")


class UnmatchedTerm(BaseModel):
    """A MeSH descriptor kept as a drug node although the packaged MeSH trees lack its name.

    The tree lookup is by preferred name, so a renamed or new descriptor can be missing;
    it is kept rather than dropped on missing evidence, and listed here.
    """

    id: str = Field(description="MeSH id (or the term itself when the record has no id).")
    label: str = Field(description="MeSH term as observed.")
    reason: Literal["unmatched_in_tree"] = "unmatched_in_tree"
    trials: int = Field(description="Trials in the result set that list this term.")


class NetworkSummary(BaseModel):
    """What a network response leaves out, and why (invariant 3: nothing is silent).

    Counts with `_total` describe the graph after drug node filtering and before the
    readability caps; `_shown` describe what the response contains. A trial "without a
    shown edge" contributed at least one edge before trimming and none after; a trial
    "without a shown node" appeared in at least one node before trimming and in none after.
    """

    excluded_terms: list[ExcludedTerm] = Field(default_factory=list)
    trials_with_excluded_terms: int = 0
    unmatched_terms: list[UnmatchedTerm] = Field(
        default_factory=list,
        description="Drug nodes kept although their descriptor is not in the MeSH trees.",
    )
    drug_class_nodes_total: int = Field(
        default=0,
        description="Nodes of type drug_class (MeSH tree numbers only in D26/D27) before the "
        "readability caps.",
    )
    drug_class_nodes_shown: int = Field(default=0, description="drug_class nodes shown.")
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
    coverage: float | None = Field(
        description="Share of total_matched the counts cover: records_retrieved / "
        "total_matched on the fetch route, 1.0 on the count_per_bucket route."
    )
    reconciled: bool = Field(
        description="Fetch route: records_retrieved equals total_matched, or the fetch was "
        "truncated. count_per_bucket route: the bucket counts sum to total_matched."
    )
    retrieval_route: RetrievalRoute = RetrievalRoute.FETCH
    count_requests: int | None = Field(
        default=None,
        description="Requests the count_per_bucket route made for this cohort (cohort total, "
        "date range, one per bucket), also when it fell back to fetching; null when it was "
        "not attempted.",
    )
    count_remainder: int | None = Field(
        default=None,
        description="total_matched minus the sum of the bucket counts; 0 when the counts "
        "reconcile. A nonzero value made the cohort fall back to fetching. Null when no "
        "bucket was counted.",
    )


class TopNSummary(BaseModel):
    """What a `top_n` limit removed from the chart.

    The Unknown bucket is never removed. `trials_omitted` counts, per cohort, the trials
    that appear only in omitted buckets (so they are not visible anywhere in the chart).
    """

    top_n: int
    omitted_keys: list[str]
    trials_omitted: dict[str, int] = Field(description="Cohort label -> omitted trials.")


class CohortPairOverlap(BaseModel):
    """Trials matched by both cohorts of one pair."""

    cohorts: list[str] = Field(min_length=2, max_length=2, description="The two cohort labels.")
    shared_trials: int = Field(ge=0)


class CohortsOverlap(BaseModel):
    """Trials that match more than one cohort and are therefore counted in each.

    Cohorts are "trials involving X", not disjoint groups: a combination trial of X and Y
    is in both. `pairs` lists every pair of cohorts (including pairs sharing 0 trials);
    `total_shared` counts distinct trials in two or more cohorts.
    """

    pairs: list[CohortPairOverlap]
    total_shared: int = Field(ge=0)


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
    records_retrieved: int = Field(
        description="Records read: every fetched record on the fetch route, the citation "
        "samples on the count_per_bucket route."
    )
    truncated: bool
    coverage: float | None = Field(
        description="Share of total_matched the counts cover, over all cohorts."
    )
    retrieval_route: RetrievalRoute = RetrievalRoute.FETCH
    cohorts: list[CohortStats] = Field(default_factory=list)
    data_quality: DataQuality = Field(
        default=DataQuality.NORMAL,
        description="'degraded' when many retrieved records were unreadable and skipped.",
    )
    top_n: TopNSummary | None = None
    cohorts_overlap: CohortsOverlap | None = Field(
        default=None,
        description="Trials shared between cohorts (fetch route; 0 when checked and none). "
        "Null for a single cohort, for networks, and when any cohort used the "
        "count_per_bucket route (only samples of its trials are known; an assumption says so).",
    )
    network: NetworkSummary | None = Field(
        default=None, description="Exclusions and readability caps; networks only."
    )
    estimated_count: int | None = Field(
        description="Trials counted via an estimated value, summed over cohorts; None for "
        "networks, and when a cohort's count was not determined (count_per_bucket route on a "
        "temporal dimension)."
    )
    unknown_date_type_count: int | None = Field(
        description="Trials placed by a date with no ACTUAL/ESTIMATED type, summed over "
        "cohorts; None unless the dimension is temporal, and when a cohort's count was not "
        "determined (count_per_bucket route)."
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
