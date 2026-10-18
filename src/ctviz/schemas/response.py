"""VisualizationResponse: the HTTP API's output, a chart spec plus full provenance."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, JsonValue

from ctviz.schemas.citation import Citation
from ctviz.schemas.llm import LlmUsage, ResponseMode
from ctviz.schemas.plan import (
    ChartType,
    Cohort,
    LlmPlanDraft,
    MatchMode,
    QueryPlan,
    RankingDirection,
)


class CountUnit(StrEnum):
    """What one unit of `count` means."""

    REGISTERED_STUDIES = "registered studies"


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


class NoticeSeverity(StrEnum):
    """How strongly a notice qualifies the chart.

    - error: the counts may be wrong (only DEGRADED_DATA); show it before the chart.
    - warning: the chart answers a narrower or different question than it seems to, or
      part of the data is missing; show it next to the chart.
    - info: context for reading the counts.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class NoticeCode(StrEnum):
    """What a warning or assumption is about; one code per kind of message the pipeline emits.

    A renderer picks banners and icons from `code` and `severity`, never from the text.
    """

    # Data quality and retrieval.
    DEGRADED_DATA = "degraded_data"
    RECORD_CAP = "record_cap"
    PAGINATION_CAP = "pagination_cap"
    NOT_RECONCILED = "not_reconciled"
    DUPLICATES_DROPPED = "duplicates_dropped"
    SKIPPED_RECORDS = "skipped_records"
    UNRECOGNIZED_VALUES = "unrecognized_values"
    NO_MATCHES = "no_matches"
    DATA_TIMESTAMP_MISSING = "data_timestamp_missing"
    COUNT_ROUTE_FALLBACK = "count_route_fallback"
    RETRIEVAL_ROUTE = "retrieval_route"
    CACHE = "cache"
    # How the question was read.
    PLAN_SOURCE = "plan_source"
    LLM_FALLBACK = "llm_fallback"
    EXPLICIT_OVERRIDE = "explicit_override"
    DRAFT_REJECTED = "draft_rejected"
    NOT_APPLIED = "not_applied"
    RANKING_DEFAULT = "ranking_default"
    # What the cohort contains.
    DRUG_MATCHED_TERMS = "drug_matched_terms"
    DID_YOU_MEAN = "did_you_mean"
    WITHDRAWN = "withdrawn"
    STATUS_TREND_BIAS = "status_trend_bias"
    COHORTS_OVERLAP = "cohorts_overlap"
    COHORTS_OVERLAP_UNKNOWN = "cohorts_overlap_unknown"
    COHORT_SUBSET = "cohort_subset"
    # Buckets.
    DIMENSION_NOTE = "dimension_note"
    BUCKETS_OVERLAP = "buckets_overlap"
    UNKNOWN_BUCKET = "unknown_bucket"
    UNKNOWN_OFF_AXIS = "unknown_off_axis"
    ESTIMATED_DATES = "estimated_dates"
    ESTIMATED_ENROLLMENT = "estimated_enrollment"
    UNTYPED_DATES = "untyped_dates"
    ESTIMATES_UNDETERMINED = "estimates_undetermined"
    INCOMPLETE_PERIOD = "incomplete_period"
    FUTURE_PERIODS = "future_periods"
    TOP_N_APPLIED = "top_n_applied"
    TOPN_TIES = "topn_ties"
    READABILITY_CAP = "readability_cap"
    OTHER_BUCKET = "other_bucket"
    CITATIONS_REDUCED = "citations_reduced"
    # Networks.
    NETWORK_NOTE = "network_note"
    NETWORK_SEMANTICS = "network_semantics"
    NETWORK_MISSING_VALUES = "network_missing_values"
    NETWORK_EXCLUDED_TERMS = "network_excluded_terms"
    NETWORK_UNMATCHED_TERMS = "network_unmatched_terms"
    NETWORK_DRUG_CLASSES = "network_drug_classes"
    NETWORK_HUB_UNDERCOUNT = "network_hub_undercount"
    NETWORK_TRIMMED = "network_trimmed"


# The severity a notice of each code has unless the emitting code raises it (e.g. a heavy
# cohort overlap is a warning, a small one an info assumption).
DEFAULT_SEVERITY: dict[NoticeCode, NoticeSeverity] = {
    **dict.fromkeys(NoticeCode, NoticeSeverity.INFO),
    NoticeCode.DEGRADED_DATA: NoticeSeverity.ERROR,
    **dict.fromkeys(
        (
            NoticeCode.RECORD_CAP,
            NoticeCode.PAGINATION_CAP,
            NoticeCode.NOT_RECONCILED,
            NoticeCode.SKIPPED_RECORDS,
            NoticeCode.UNRECOGNIZED_VALUES,
            NoticeCode.NO_MATCHES,
            NoticeCode.DATA_TIMESTAMP_MISSING,
            NoticeCode.NOT_APPLIED,
            NoticeCode.DID_YOU_MEAN,
            NoticeCode.STATUS_TREND_BIAS,
            NoticeCode.COHORT_SUBSET,
            NoticeCode.TOPN_TIES,
            NoticeCode.READABILITY_CAP,
            NoticeCode.FUTURE_PERIODS,
            NoticeCode.NETWORK_SEMANTICS,
        ),
        NoticeSeverity.WARNING,
    ),
}


class Notice(BaseModel):
    """One warning or assumption: machine-readable `code` and `severity`, readable `message`.

    `cohort` names the cohort the notice is about (None when it is about the whole
    response); in a comparison the message also starts with "<cohort>: ".
    """

    code: NoticeCode
    severity: NoticeSeverity
    cohort: str | None = None
    message: str

    @classmethod
    def of(
        cls,
        code: NoticeCode,
        message: str,
        *,
        cohort: str | None = None,
        severity: NoticeSeverity | None = None,
    ) -> Notice:
        """A notice with the code's default severity unless `severity` is given."""
        return cls(
            code=code,
            severity=severity or DEFAULT_SEVERITY[code],
            cohort=cohort,
            message=message,
        )


class FieldType(StrEnum):
    """Vega-Lite measurement type of an encoded field."""

    NOMINAL = "nominal"
    TEMPORAL = "temporal"
    QUANTITATIVE = "quantitative"


ESTIMATED_COUNT_DESCRIPTION = (
    "Studies counted here via an estimated value: an ESTIMATED start date (start_year, "
    "start_month) or an ESTIMATED enrollment count (enrollment_bucket). Null on dimensions "
    "without an estimate concept, and when not determined (count_per_bucket route)."
)
UNKNOWN_DATE_TYPE_COUNT_DESCRIPTION = (
    "Studies placed here by a start date with no ACTUAL/ESTIMATED type (legacy records). "
    "Null unless the dimension is a start date, and when not determined (count_per_bucket "
    "route)."
)
CITED_TRIALS_TOTAL_DESCRIPTION = (
    "Distinct studies (NCT IDs) cited before the cap. The citation cap applies to primary "
    "citations; each kept study keeps its supporting citations."
)
CITATION_COUNT_TOTAL_DESCRIPTION = (
    "Citations (primary and supporting) before the cap. Larger than cited_trials_total when "
    "a study is cited more than once, e.g. intervention names supporting a MeSH term, or "
    "both endpoints of an edge."
)


class Datum(BaseModel):
    """One bar or point: a dimension value and the number of studies in it."""

    key: str = Field(description="Dimension value as grouped, e.g. 'Phase 1/Phase 2' or '2024'.")
    label: str
    cohort: str | None = None
    count: int
    estimated_count: int | None = Field(default=None, description=ESTIMATED_COUNT_DESCRIPTION)
    unknown_date_type_count: int | None = Field(
        default=None, description=UNKNOWN_DATE_TYPE_COUNT_DESCRIPTION
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
    is_not_applicable: bool = Field(
        default=False,
        description="Bucket of studies for which the dimension does not apply (phase 'Not "
        "Applicable', and observational / expanded-access studies without a phase).",
    )
    is_other: bool = Field(
        default=False,
        description="The 'Other (N values)' bucket of a top_n/bottom_n chart on a "
        "single-valued dimension: the exact number of studies in the omitted values.",
    )
    citations: list[Citation] = Field(default_factory=list)
    cited_trials_total: int = Field(description=CITED_TRIALS_TOTAL_DESCRIPTION)
    citation_count_total: int = Field(description=CITATION_COUNT_TOTAL_DESCRIPTION)


class NetworkNode(BaseModel):
    """A network node; `size` is the number of trials the entity appears in."""

    id: str
    label: str
    type: NodeType
    size: int
    citations: list[Citation] = Field(default_factory=list)
    cited_trials_total: int = Field(description=CITED_TRIALS_TOTAL_DESCRIPTION)
    citation_count_total: int = Field(description=CITATION_COUNT_TOTAL_DESCRIPTION)


class NetworkEdge(BaseModel):
    """An undirected edge; `weight` is the number of trials where both endpoints co-occur."""

    source: str
    target: str
    weight: int
    citations: list[Citation] = Field(default_factory=list)
    cited_trials_total: int = Field(description=CITED_TRIALS_TOTAL_DESCRIPTION)
    citation_count_total: int = Field(description=CITATION_COUNT_TOTAL_DESCRIPTION)


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
    node_supporting_citations_dropped: bool = Field(
        default=False,
        description="True when, after the edge cut, nodes also dropped their supporting "
        "citations to fit the size budget (primary citations are kept).",
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
        description="top_n or bottom_n chosen by code (the configured default) because the "
        "question asked for a ranking (most or fewest) without giving a number; None "
        "otherwise. The plan's top_n / bottom_n says which end.",
    )
    unhandled_terms: list[str] = Field(
        default_factory=list,
        description="Parts of the question the plan does not apply (no filter, dimension or "
        "relation expresses them), as flagged by the LLM. Each is also a 'Not applied' "
        "warning: the counts include trials regardless of these terms.",
    )
    planning_ms: float | None = Field(default=None, description="Planner wall-clock time.")


class MatchedTerm(BaseModel):
    """One intervention MeSH term containing a cohort's drug name, and its trial count."""

    term: str
    trials: int = Field(ge=0)


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
    drug_matched_terms: list[MatchedTerm] | None = Field(
        default=None,
        description="Distinct intervention MeSH terms in the counted records whose name "
        "contains the cohort's drug filter (case-insensitive), with trial counts, most "
        "first. More than one term usually means several molecules (conjugates, "
        "biosimilars, tracers). Null without a drug filter, or on the count_per_bucket "
        "route, where the records are not read.",
    )
    withdrawn: int | None = Field(
        default=None,
        description="Counted studies whose overallStatus is WITHDRAWN (never enrolled). "
        "Null on the count_per_bucket route, where the records are not read.",
    )


class TopNSummary(BaseModel):
    """What a `top_n` or `bottom_n` limit removed from the chart.

    The Unknown bucket is never removed. `trials_omitted` counts, per cohort, the trials
    that appear only in omitted buckets (so they are not visible anywhere in the chart).
    """

    top_n: int = Field(description="Number of known buckets kept (at the `direction` end).")
    direction: RankingDirection = Field(
        default=RankingDirection.MOST,
        description="'most': the N buckets with the most trials were kept (top_n); "
        "'fewest': the N with the fewest (bottom_n).",
    )
    omitted_keys: list[str]
    trials_omitted: dict[str, int] = Field(description="Cohort label -> omitted trials.")
    cutoff_count: int | None = Field(
        default=None,
        description="Study count of the last kept value (the ranking cutoff), over all "
        "cohorts; None when no known value was kept.",
    )
    omitted_ties: int = Field(
        default=0,
        description="Omitted values whose count equals cutoff_count: they tie with a shown "
        "value and were left out only by the display order.",
    )


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
    count_unit: CountUnit = Field(
        description="What one unit of `count` is: a study registered on ClinicalTrials.gov "
        "of any study type (interventional, observational, expanded access) and any overall "
        "status, including withdrawn studies, unless a filter narrows it."
    )
    count_scope: str = Field(
        description="Which studies the counts cover, built from the executed filters: "
        "study types, primary purposes and status, e.g. 'interventional studies, excluding "
        "withdrawn' or 'any study type, any status'; per cohort ('<label>: ...; ...') when "
        "the cohorts differ."
    )
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
        description="Datum estimated_count summed over cohorts: "
        + ESTIMATED_COUNT_DESCRIPTION
        + " Null for networks."
    )
    unknown_date_type_count: int | None = Field(
        description="Datum unknown_date_type_count summed over cohorts: "
        + UNKNOWN_DATE_TYPE_COUNT_DESCRIPTION
    )
    data_timestamp: datetime | None = Field(description="API dataTimestamp from /version.")
    assumptions: list[Notice] = Field(
        default_factory=list, description="How the counts were made; mostly severity info."
    )
    warnings: list[Notice] = Field(
        default_factory=list,
        description="What qualifies the counts. A degraded_data error is always first.",
    )
    timing_ms: dict[str, float] = Field(default_factory=dict)
    request_id: str | None = Field(
        default=None, description="The X-Request-ID of the /query request; None outside HTTP."
    )
    llm: LlmUsage = Field(
        default_factory=LlmUsage,
        description="Every LLM call made for this request, with totals; no calls when the "
        "plan came from structured fields only.",
    )


class VisualizationResponse(BaseModel):
    """Top-level response body."""

    visualization: Visualization
    meta: Meta


class ErrorResponse(BaseModel):
    """Structured error body returned instead of a chart (invariant 7)."""

    code: str = Field(description="Machine-readable error code.")
    message: str
    suggestions: list[str] = Field(default_factory=list)
