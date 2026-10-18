"""The fixed pipeline: validate -> retrieve -> normalize -> aggregate -> spec.

The planner runs before it (see `api.app`) and hands over an `Interpretation`. Each
stage is called once per request and timed. Facts that the chart alone cannot show
(how the plan was made, explicit overrides, the retrieval route, truncation,
reconciliation, duplicates, skipped and unrecognized records, data quality, cache use)
become meta warnings or assumptions here (invariants 3 and 6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from ctviz.aggregate import build_network, counted_sample_problems, group_cohorts
from ctviz.aggregate.drug_terms import matched_terms
from ctviz.aggregate.registry import DIMENSIONS, DimensionKind
from ctviz.config import (
    DEFAULT_MAX_BARS,
    CountRoutePolicy,
    DataQualityThresholds,
    NetworkLimits,
)
from ctviz.errors import (
    InvalidFilterError,
    RateLimitError,
    RetrievalError,
    SourceRecordError,
    UpstreamDataFormatError,
    UpstreamError,
)
from ctviz.normalize import normalize_study, unrecognized_warnings
from ctviz.planner import validate_plan
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.query_builder import (
    RETRIEVAL_SORT_DIRECTION,
    RETRIEVAL_SORT_FIELD,
    search_semantics,
    study_query_params,
)
from ctviz.retrieval_route import (
    CohortRetrieval,
    CountAttempt,
    count_threshold,
    fallback,
    retrieve_cohort,
)
from ctviz.schemas.aggregate import CountedBucket, CountedCohort, Graph, GroupedCohorts
from ctviz.schemas.llm import LlmUsage
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan, RankingDirection
from ctviz.schemas.response import (
    CohortStats,
    DataQuality,
    Interpretation,
    MatchedTerm,
    Notice,
    NoticeCode,
    RetrievalRoute,
    VisualizationResponse,
)
from ctviz.schemas.retrieval import CountResult, FetchResult
from ctviz.schemas.trial import OverallStatus, TrialRecord
from ctviz.spec import build_response
from ctviz.text import plural

logger = logging.getLogger(__name__)

# Matched drug terms named in the warning sentence; meta.cohorts lists all of them.
MATCHED_TERMS_SHOWN = 5
# Skipped records listed one per line in meta.warnings; the rest are summed in one line
# and logged in full (engineering review M5: the list was unbounded).
SKIPPED_RECORDS_SHOWN = 10
# A drug cohort this small whose records carry a MeSH term other than the typed name
# gets one extra count request for that term ("did you mean"), e.g. 'pembro' -> 2 studies
# whose MeSH term pembrolizumab matches 2,642.
DID_YOU_MEAN_BELOW = 20


class SkipBand(StrEnum):
    """What a cohort's share of unreadable (structurally skipped) records means."""

    NORMAL = "normal"
    DEGRADED = "degraded"
    REFUSE = "refuse"


@dataclass
class StageTimer:
    """Wall-clock milliseconds per pipeline stage."""

    timings: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time the enclosed block under `name`."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = round((time.perf_counter() - started) * 1000, 1)


@dataclass
class CohortData:
    """One cohort's retrieval and the records that survived normalization.

    On the fetch route `records` are every fetched trial; on the count_per_bucket route
    `counted` holds the API's bucket counts and `records` are their citation samples.
    """

    cohort: Cohort
    retrieval: CohortRetrieval
    records: list[TrialRecord]
    skipped: list[SourceRecordError]
    counted: CountedCohort | None = None

    @property
    def fetch(self) -> FetchResult | None:
        """The fetch-route result; None on the count_per_bucket route."""
        return self.retrieval.fetch

    @property
    def route(self) -> RetrievalRoute:
        """The route whose counts this cohort reports."""
        return RetrievalRoute.FETCH if self.counted is None else RetrievalRoute.COUNT_PER_BUCKET

    @property
    def total_count(self) -> int:
        """totalCount the API reported for the cohort."""
        return self.retrieval.total_count

    @property
    def truncated(self) -> bool:
        """True when the record cap cut the fetch."""
        return self.fetch is not None and self.fetch.truncated

    @property
    def retrieved(self) -> int:
        """Records read for this cohort: fetched (deduplicated) records, or citation samples."""
        return len(self.records) + len(self.skipped)


async def run(
    plan: QueryPlan,
    *,
    question: str,
    client: CtGovClient,
    as_of: date | None = None,
    network_limits: NetworkLimits | None = None,
    data_quality_thresholds: DataQualityThresholds | None = None,
    count_route: CountRoutePolicy | None = None,
) -> VisualizationResponse:
    """Answer a plan supplied directly by code (no planner; planned_by "structured_fields")."""
    interpretation = Interpretation(question=question, plan=plan, planned_by="structured_fields")
    return await execute(
        interpretation,
        client=client,
        as_of=as_of,
        network_limits=network_limits,
        data_quality_thresholds=data_quality_thresholds,
        count_route=count_route,
    )


async def execute(
    interpretation: Interpretation,
    *,
    client: CtGovClient,
    as_of: date | None = None,
    network_limits: NetworkLimits | None = None,
    data_quality_thresholds: DataQualityThresholds | None = None,
    count_route: CountRoutePolicy | None = None,
    request_id: str | None = None,
    llm_usage: LlmUsage | None = None,
    max_bars: int = DEFAULT_MAX_BARS,
) -> VisualizationResponse:
    """Answer the planner's plan with live data.

    Raises PlanValidationError or InvalidFilterError before any network call, and
    RetrievalError subclasses if /studies cannot be fetched; a failing /version only
    nulls `meta.data_timestamp`, with a warning naming the cause. A record that fails to
    normalize is skipped and reported, never fatal by itself; a record with an
    unrecognized value is kept. When a cohort's share of skipped records reaches the
    refuse threshold, UpstreamDataFormatError is raised instead of returning a chart;
    at the degraded threshold the response is marked `data_quality="degraded"`. A fetch
    cut by the record cap marks it `data_quality="partial"` (over degraded, whose warning
    stays first) and the spec stage says so in the title.
    A cohort matching more trials than the record cap, grouped by an API-countable
    dimension, is counted exactly per bucket instead (`retrieval_route.retrieve_cohort`);
    when its sample records contradict their buckets it is fetched after all
    (timed as "retrieve_fallback").
    `network_limits`, `data_quality_thresholds`, `count_route` and `max_bars` (the
    readability cap on categorical charts) default to the configured defaults.
    `request_id` and `llm_usage` (the planner's LLM calls) are copied into meta; without
    them meta.llm lists no calls.
    """
    plan, question = interpretation.plan, interpretation.question
    limits = network_limits or NetworkLimits()
    thresholds = data_quality_thresholds or DataQualityThresholds()
    policy = count_route or CountRoutePolicy()
    timer = StageTimer()
    started = time.perf_counter()

    with timer.stage("validate"):
        validate_plan(plan)
        cohort_params = [study_query_params(c.filters, plan.match_mode) for c in plan.cohorts]

    with timer.stage("retrieve"):
        retrievals = [
            await retrieve_cohort(params, plan.dimension, client, policy)
            for params in cohort_params
        ]
        data_timestamp, timestamp_problem = await fetch_data_timestamp(client)

    with timer.stage("normalize"):
        # Normalizing thousands of records is CPU work; a thread keeps the event loop
        # (other requests, /health) responsive meanwhile.
        cohorts = [
            await asyncio.to_thread(normalize_cohort, cohort, retrieval, plan.dimension)
            for cohort, retrieval in zip(plan.cohorts, retrievals, strict=True)
        ]

    rejected = [index for index, data in enumerate(cohorts) if sample_problems(data, plan)]
    if rejected:
        with timer.stage("retrieve_fallback"):
            for index in rejected:
                cohorts[index] = await refetch_cohort(
                    cohorts[index],
                    cohort_params[index],
                    client,
                    sample_problems(cohorts[index], plan),
                )

    data_quality = assess_data_quality(cohorts, thresholds)

    terms = [suggested_term(data) for data in cohorts]
    suggestions: list[tuple[str, str]] = []
    if any(terms):
        with timer.stage("suggest"):
            suggestions = [
                await did_you_mean(data, term, plan, client)
                for data, term in zip(cohorts, terms, strict=True)
                if term is not None
            ]

    with timer.stage("aggregate"):
        result = await asyncio.to_thread(aggregate, plan, cohorts, as_of, limits, max_bars)

    with timer.stage("spec"):
        response = build_response(
            plan,
            result,
            question=question,
            cohort_stats=[cohort_stats(data) for data in cohorts],
            retrieval_route=combined_route(cohorts),
            search_semantics=merged_search_semantics(plan),
            data_timestamp=data_timestamp,
            timing_ms={},
            warnings=[
                *data_quality_warnings(cohorts, data_quality, thresholds),
                *unhandled_term_warnings(interpretation),
                *(
                    Notice.of(NoticeCode.DID_YOU_MEAN, text, cohort=label)
                    for label, text in suggestions
                ),
                *(
                    Notice.of(NoticeCode.STATUS_TREND_BIAS, text)
                    for text in status_trend_warnings(plan)
                ),
                *(
                    Notice.of(NoticeCode.DATA_TIMESTAMP_MISSING, text)
                    for text in data_timestamp_warnings(timestamp_problem)
                ),
                *(w for data in cohorts for w in retrieval_warnings(data, len(cohorts))),
            ],
            assumptions=[
                *planning_assumptions(interpretation),
                *ranking_assumptions(interpretation, result),
                *(
                    a
                    for data in cohorts
                    for a in retrieval_assumptions(data, plan, len(cohorts), client, policy)
                ),
            ],
            interpretation=interpretation,
            max_response_bytes=limits.max_response_bytes,
            reduced_edge_citation_cap=limits.reduced_edge_citation_cap,
        )

    timer.timings["total"] = round((time.perf_counter() - started) * 1000, 1)
    response.meta.timing_ms = timer.timings
    response.meta.request_id = request_id
    response.meta.llm = llm_usage or LlmUsage()
    response.meta.data_quality = (
        DataQuality.PARTIAL if any(data.truncated for data in cohorts) else data_quality
    )
    return response


def normalize_cohort(
    cohort: Cohort, retrieval: CohortRetrieval, dimension: Dimension | None
) -> CohortData:
    """Normalize a cohort's fetched records, or each counted bucket's sample records."""
    if retrieval.count is None:
        if retrieval.fetch is None:
            raise ValueError("A retrieval without counts must hold fetched records.")
        return CohortData(cohort, retrieval, *normalize_all(retrieval.fetch.studies))
    if dimension is None:
        raise ValueError("Only a dimension can be counted per bucket.")
    buckets: list[CountedBucket] = []
    records: list[TrialRecord] = []
    skipped: list[SourceRecordError] = []
    for bucket in retrieval.count.buckets:
        bucket_records, bucket_skipped = normalize_all(bucket.studies)
        buckets.append(
            CountedBucket(key=bucket.key, count=bucket.total_count, records=bucket_records)
        )
        records.extend(bucket_records)
        skipped.extend(bucket_skipped)
    counted = CountedCohort(total_count=retrieval.total_count, buckets=buckets)
    return CohortData(cohort, retrieval, records, skipped, counted)


def sample_problems(data: CohortData, plan: QueryPlan) -> list[str]:
    """Why a counted cohort's samples contradict their buckets; [] on the fetch route."""
    if data.counted is None or plan.dimension is None:
        return []
    return counted_sample_problems(data.counted, plan.dimension)


async def refetch_cohort(
    data: CohortData, params: dict[str, str], client: CtGovClient, problems: list[str]
) -> CohortData:
    """Fetch a counted cohort after all, because its samples contradict their buckets.

    A sample record outside its bucket means the bucket filter does not select what the
    normalizer calls that bucket, so the counts cannot be trusted.
    """
    shown = "; ".join(problems[:3])
    more = f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""
    attempt = data.retrieval.attempt or CountAttempt(requests=0)
    retrieval = await fallback(
        client,
        params,
        data.total_count,
        attempt,
        f"sample records contradict their bucket filters: {shown}{more}",
    )
    if retrieval.fetch is None:
        raise ValueError("A fallback retrieval always fetches records.")
    records, skipped = await asyncio.to_thread(normalize_all, retrieval.fetch.studies)
    return CohortData(data.cohort, retrieval, records, skipped)


def combined_route(cohorts: Sequence[CohortData]) -> RetrievalRoute:
    """The route of every cohort, or "mixed" when they differ."""
    routes = {data.route for data in cohorts}
    return routes.pop() if len(routes) == 1 else RetrievalRoute.MIXED


def planning_assumptions(interpretation: Interpretation) -> list[Notice]:
    """How the plan was made: who read the question, and what explicit fields replaced."""
    if interpretation.planned_by == "rules":
        return [
            Notice.of(
                NoticeCode.PLAN_SOURCE,
                "The question text was not interpreted (no LLM planner is configured); the "
                "request's structured fields alone define the query.",
            )
        ]
    if interpretation.planned_by == "structured_fields":
        return [
            Notice.of(
                NoticeCode.PLAN_SOURCE,
                "The question text was not interpreted; the plan was supplied directly.",
            )
        ]
    messages = [
        Notice.of(
            NoticeCode.PLAN_SOURCE,
            f"The plan was drafted from the question by the LLM ({interpretation.model_used} "
            f"via {interpretation.provider}) and validated; meta.interpretation.draft holds "
            "the draft. Counts come from the registry.",
        )
    ]
    if interpretation.fallback_reason is not None:
        messages.append(
            Notice.of(
                NoticeCode.LLM_FALLBACK,
                f"The primary LLM provider did not answer, so a fallback provider "
                f"({interpretation.provider}) drafted the plan: {interpretation.fallback_reason}",
            )
        )
    messages.extend(
        Notice.of(
            NoticeCode.EXPLICIT_OVERRIDE,
            f"The explicit request field set `{override.path}` to "
            f"{json.dumps(override.explicit_value)}; the LLM had read "
            f"{json.dumps(override.llm_value)} from the question.",
        )
        for override in interpretation.overrides
    )
    messages.extend(
        Notice.of(
            NoticeCode.DRAFT_REJECTED,
            f"An earlier LLM draft was rejected and redrafted: {problem}",
        )
        for problem in interpretation.rejected_attempts
    )
    return messages


def unhandled_term_warnings(interpretation: Interpretation) -> list[Notice]:
    """Name each part of the question the plan does not apply, so the counts are not read
    as answering it (invariant 3). Placed before every warning except degraded data."""
    return [
        Notice.of(
            NoticeCode.NOT_APPLIED,
            f"Not applied: '{term}' — no such filter exists; counts include all matching trials.",
        )
        for term in interpretation.unhandled_terms
    ]


def status_trend_warnings(plan: QueryPlan) -> list[str]:
    """Warn that a status filter on a time axis cannot show a trend.

    Status is today's status: trials that started years ago have mostly left
    "recruiting" (or any other status), so a status-filtered time series always leans
    toward recent years whatever the real activity was.
    """
    if plan.dimension is None or DIMENSIONS[plan.dimension].kind != DimensionKind.TEMPORAL:
        return []
    if not any(cohort.filters.statuses for cohort in plan.cohorts):
        return []
    return [
        "Status reflects today; older trials have left this status, so earlier years are "
        "under-represented. This is not a trend in activity."
    ]


def ranking_assumptions(
    interpretation: Interpretation, result: GroupedCohorts | Graph
) -> list[Notice]:
    """State that the planner chose top_n or bottom_n because the question asked for a ranking."""
    count = interpretation.ranking_top_n
    if count is None or not isinstance(result, GroupedCohorts) or result.top_n is None:
        return []
    omitted = len(result.top_n.omitted_keys)
    if result.top_n.direction == RankingDirection.FEWEST:
        text = (
            f"Question asked for the fewest; showing bottom {count} (configurable); "
            f"{omitted} buckets omitted; Unknown and Unrecognized buckets are not ranked but "
            "still shown; values with no matching trials have no bucket and cannot appear"
        )
    else:
        text = (
            f"Question asked for a ranking; showing top {count} (configurable); "
            f"{omitted} buckets omitted"
        )
    return [Notice.of(NoticeCode.RANKING_DEFAULT, text)]


def normalize_all(
    studies: Sequence[Any],
) -> tuple[list[TrialRecord], list[SourceRecordError]]:
    """Normalize every study; collect the ones that cannot be read (structural failures)."""
    records: list[TrialRecord] = []
    skipped: list[SourceRecordError] = []
    for study in studies:
        try:
            records.append(normalize_study(study))
        except SourceRecordError as error:
            skipped.append(error)
    return records, skipped


def classify_skip_rate(retrieved: int, skipped: int, thresholds: DataQualityThresholds) -> SkipBand:
    """Band of one cohort's skip ratio (structurally skipped / retrieved).

    Fewer than `min_skipped_for_degraded` skipped records is always normal, so a small
    sample with a few bad records is not flagged; otherwise the ratio is compared with
    the refuse and degraded thresholds (both inclusive).
    """
    if retrieved == 0 or skipped < thresholds.min_skipped_for_degraded:
        return SkipBand.NORMAL
    ratio = skipped / retrieved
    if ratio >= thresholds.refuse_skip_ratio:
        return SkipBand.REFUSE
    if ratio >= thresholds.degraded_skip_ratio:
        return SkipBand.DEGRADED
    return SkipBand.NORMAL


def assess_data_quality(
    cohorts: Sequence[CohortData], thresholds: DataQualityThresholds
) -> DataQuality:
    """Log each cohort's skip rate and band; refuse, or mark degraded, per the worst cohort.

    The log line is the per-request skip rate a baseline-relative anomaly detector would
    consume (docs/decisions.md). Raises UpstreamDataFormatError for a refused cohort.
    """
    quality = DataQuality.NORMAL
    for data in cohorts:
        band = classify_skip_rate(data.retrieved, len(data.skipped), thresholds)
        ratio = len(data.skipped) / data.retrieved if data.retrieved else 0.0
        logger.info(
            "normalize cohort=%r retrieved=%d skipped=%d skip_ratio=%.4f unrecognized=%d band=%s",
            data.cohort.label,
            data.retrieved,
            len(data.skipped),
            ratio,
            sum(1 for record in data.records if record.unrecognized_values),
            band.value,
        )
        if band == SkipBand.REFUSE:
            error = UpstreamDataFormatError(
                data.cohort.label, data.retrieved, len(data.skipped), thresholds.refuse_skip_ratio
            )
            logger.error("%s", error)
            raise error
        if band == SkipBand.DEGRADED:
            quality = DataQuality.DEGRADED
    return quality


def data_quality_warnings(
    cohorts: Sequence[CohortData], quality: DataQuality, thresholds: DataQualityThresholds
) -> list[Notice]:
    """The prominent warning for a degraded result, placed first in meta.warnings."""
    if quality != DataQuality.DEGRADED:
        return []
    degraded = [
        data
        for data in cohorts
        if classify_skip_rate(data.retrieved, len(data.skipped), thresholds) == SkipBand.DEGRADED
    ]
    return [
        cohort_notice(
            data,
            len(cohorts),
            NoticeCode.DEGRADED_DATA,
            f"DEGRADED DATA: {len(data.skipped)} of {data.retrieved} retrieved trials "
            f"({len(data.skipped) / data.retrieved:.1%}) could not be read and are not counted "
            f"(DEGRADED_SKIP_RATIO={thresholds.degraded_skip_ratio:g}). The API's data format "
            "may have changed; counts and rankings may be off by up to the skipped number.",
        )
        for data in degraded
    ]


async def fetch_data_timestamp(client: CtGovClient) -> tuple[datetime | None, str | None]:
    """The API's dataTimestamp, or None and why it is unavailable.

    /version only labels when the data was refreshed; the counts come from /studies.
    So a /version that fails after its retries (5xx, network error, rate limiting, a
    non-object body) or gives no valid timestamp is reported, never fatal.
    """
    try:
        timestamp = await client.data_timestamp()
    except (UpstreamError, RateLimitError) as error:
        logger.warning("No dataTimestamp for this response: %s", error)
        return None, f"the request failed: {error}"
    if timestamp is None:
        return None, "it returned no valid dataTimestamp"
    return timestamp, None


def data_timestamp_warnings(problem: str | None) -> list[str]:
    """State that the data's as-of time is unknown, and why, when /version gave none."""
    if problem is None:
        return []
    return [
        f"ClinicalTrials.gov /version gave no data timestamp ({problem}), so "
        "meta.data_timestamp is null: the counts are unaffected, but the time of the API's "
        "last data refresh is not known."
    ]


def aggregate(
    plan: QueryPlan,
    cohorts: Sequence[CohortData],
    as_of: date | None,
    limits: NetworkLimits,
    max_bars: int,
) -> GroupedCohorts | Graph:
    """Dispatch to the network builder or the per-cohort grouping (readability-capped)."""
    if plan.relation is not None:
        return build_network(
            cohorts[0].records,
            plan.relation,
            max_nodes=limits.max_nodes,
            min_edge_weight=limits.min_edge_weight,
            queried_drug=cohorts[0].cohort.filters.drug,
        )
    if plan.dimension is None:
        raise ValueError("A validated plan has a dimension or a relation.")
    return group_cohorts(
        [
            (data.cohort.label, data.records if data.counted is None else data.counted)
            for data in cohorts
        ],
        plan.dimension,
        top_n=plan.top_n,
        bottom_n=plan.bottom_n,
        as_of=as_of,
        max_bars=max_bars,
    )


def cohort_stats(data: CohortData) -> CohortStats:
    """Retrieval accounting for one cohort.

    On the count_per_bucket route the counts cover every match (coverage 1.0) and are
    reconciled because the bucket counts summed to totalCount before they were used.
    """
    attempt = data.retrieval.attempt
    fetch = data.fetch
    return CohortStats(
        label=data.cohort.label,
        total_matched=data.total_count,
        records_retrieved=data.retrieved,
        records_skipped=len(data.skipped),
        truncated=data.truncated,
        coverage=(1.0 if data.total_count else None) if fetch is None else fetch.coverage,
        reconciled=True if fetch is None else fetch.reconciled,
        retrieval_route=data.route,
        count_requests=attempt.requests if attempt is not None else None,
        count_remainder=attempt.remainder if attempt is not None else None,
        drug_matched_terms=drug_matched_terms(data),
        withdrawn=None if fetch is None else withdrawn_count(data.records),
    )


def drug_matched_terms(data: CohortData) -> list[MatchedTerm] | None:
    """The cohort's MeSH terms containing its drug filter; None without a drug filter or
    on the count_per_bucket route (only bucket samples were read)."""
    drug = data.cohort.filters.drug
    if drug is None or data.fetch is None:
        return None
    return matched_terms(data.records, drug)


def withdrawn_count(records: Sequence[TrialRecord]) -> int:
    """Records whose overall status is WITHDRAWN: registered, but never enrolled."""
    return sum(1 for record in records if record.overall_status == OverallStatus.WITHDRAWN)


def merged_search_semantics(plan: QueryPlan) -> dict[str, str]:
    """Search semantics of every filter used by any cohort."""
    semantics: dict[str, str] = {}
    for cohort in plan.cohorts:
        semantics.update(search_semantics(cohort.filters, plan.match_mode))
    return semantics


def prefixed(data: CohortData, cohort_count: int, message: str) -> str:
    """Name the cohort when several cohorts share one meta block."""
    return f"{data.cohort.label}: {message}" if cohort_count > 1 else message


def cohort_notice(data: CohortData, cohort_count: int, code: NoticeCode, message: str) -> Notice:
    """A notice about one cohort: `cohort` set, and the message prefixed in a comparison."""
    return Notice.of(code, prefixed(data, cohort_count, message), cohort=data.cohort.label)


def suggested_term(data: CohortData) -> str | None:
    """The MeSH term to suggest for a tiny drug cohort, or None.

    'pembro' matches 2 studies whose MeSH term is pembrolizumab. Only below
    DID_YOU_MEAN_BELOW matches, only on the fetch route (the only one that reads every
    record), and only when no matched term is the typed name itself: then the user got
    the term they asked for. The term most matched records carry is suggested.
    """
    drug = data.cohort.filters.drug
    if drug is None or data.total_count >= DID_YOU_MEAN_BELOW:
        return None
    terms = drug_matched_terms(data) or []
    typed = drug.strip().casefold()
    if not terms or any(term.term.casefold() == typed for term in terms):
        return None
    return terms[0].term


async def did_you_mean(
    data: CohortData, term: str, plan: QueryPlan, client: CtGovClient
) -> tuple[str, str]:
    """(cohort label, hint) naming `term` and its own study count.

    The count is one countTotal request through the client's rate limiter, with the
    cohort's other filters unchanged; it comes from the API, never estimated. When it
    cannot be retrieved the hint says so instead of failing the request.
    """
    drug = data.cohort.filters.drug or ""
    filters = data.cohort.filters.model_copy(update={"drug": term})
    head = (
        f"'{drug}' matched {plural(data.total_count, 'study', 'studies')}; the matched records "
        f"carry the intervention MeSH term {term}"
    )
    try:
        sample = await client.sample(study_query_params(filters, plan.match_mode), 1)
    except (RetrievalError, InvalidFilterError) as error:
        logger.warning("No count for suggested term %r: %s", term, error)
        return data.cohort.label, (
            f"{head} — did you mean that? (Its study count could not be retrieved: {error}.)"
        )
    total = sample.total_count
    has_other_filters = filters.model_copy(update={"drug": None}) != Filters()
    scope = " with the same other filters" if has_other_filters else ""
    studies = f"{total:,} {'study' if total == 1 else 'studies'}"
    return data.cohort.label, f"{head} ({studies}{scope}) — did you mean that?"


def retrieval_warnings(data: CohortData, cohort_count: int) -> list[Notice]:
    """Empty results, a rejected count route, truncation, reconciliation failures,
    duplicates, skipped records and unrecognized values."""
    retrieved = data.retrieved
    messages: list[tuple[NoticeCode, str]] = []
    if data.total_count == 0:
        messages.append((NoticeCode.NO_MATCHES, "No trials matched these filters."))
    attempt = data.retrieval.attempt
    if attempt is not None and attempt.fallback_reason is not None:
        messages.append(
            (
                NoticeCode.COUNT_ROUTE_FALLBACK,
                "Exact per-bucket counting (retrieval_route count_per_bucket) was not used: "
                f"{attempt.fallback_reason}; the records were fetched instead.",
            )
        )
    fetch = data.fetch
    if fetch is None:
        messages.extend(counted_sample_warnings(data))
        messages.extend(
            (NoticeCode.UNRECOGNIZED_VALUES, text) for text in unrecognized_warnings(data.records)
        )
        return [cohort_notice(data, cohort_count, code, text) for code, text in messages]
    if fetch.truncated and fetch.coverage is not None:
        messages.append(
            (
                NoticeCode.RECORD_CAP,
                f"Only the first {retrieved} of {fetch.total_count} matching trials were "
                f"retrieved (record cap); counts cover {fetch.coverage:.1%} of matches.",
            )
        )
    if fetch.page_cap_hit:
        messages.append(
            (
                NoticeCode.PAGINATION_CAP,
                f"Pagination stopped after {fetch.pages} pages (the page cap) although the API "
                f"kept returning a next-page token; {retrieved} of {fetch.total_count} "
                "matching trials were retrieved and counted.",
            )
        )
    if not fetch.reconciled:
        messages.append(
            (
                NoticeCode.NOT_RECONCILED,
                f"Retrieved {retrieved} trials but the API reported {fetch.total_count} "
                "matches; the data may have changed during pagination, so counts can be off "
                "by the difference.",
            )
        )
    if fetch.duplicates_dropped:
        messages.append(
            (
                NoticeCode.DUPLICATES_DROPPED,
                f"{fetch.duplicates_dropped} duplicate records (same NCT ID on several pages) "
                "were dropped.",
            )
        )
    if data.skipped:
        messages.append(
            (
                NoticeCode.SKIPPED_RECORDS,
                f"{len(data.skipped)} of {retrieved} retrieved trials were skipped because the "
                "record could not be read; they are not counted.",
            )
        )
        messages.extend(skipped_record_lines(data.skipped))
    messages.extend(
        (NoticeCode.UNRECOGNIZED_VALUES, text) for text in unrecognized_warnings(data.records)
    )
    messages.extend(cohort_content_warnings(data))
    return [cohort_notice(data, cohort_count, code, text) for code, text in messages]


def skipped_record_lines(skipped: Sequence[SourceRecordError]) -> list[tuple[NoticeCode, str]]:
    """One line per skipped record up to SKIPPED_RECORDS_SHOWN, then one 'and N more' line.

    Every skipped record is logged, so the full list survives the cap.
    """
    for error in skipped:
        logger.info("Skipped record: %s", error)
    lines = [(NoticeCode.SKIPPED_RECORDS, f"Skipped {error}") for error in skipped]
    if len(skipped) <= SKIPPED_RECORDS_SHOWN:
        return lines
    more = len(skipped) - SKIPPED_RECORDS_SHOWN
    return [
        *lines[:SKIPPED_RECORDS_SHOWN],
        (
            NoticeCode.SKIPPED_RECORDS,
            f"... and {more} more skipped records (the full list is in the server log).",
        ),
    ]


def cohort_content_warnings(data: CohortData) -> list[tuple[NoticeCode, str]]:
    """What the counted records contain that the filters do not say: several molecules
    behind one drug name, and withdrawn studies that never enrolled."""
    messages: list[tuple[NoticeCode, str]] = []
    drug = data.cohort.filters.drug
    terms = drug_matched_terms(data) or []
    if drug is not None and len(terms) > 1:
        shown = ", ".join(f"{term.term} ({term.trials:,})" for term in terms[:MATCHED_TERMS_SHOWN])
        more = ", ..." if len(terms) > MATCHED_TERMS_SHOWN else ""
        messages.append(
            (
                NoticeCode.DRUG_MATCHED_TERMS,
                f"'{drug}' matched {len(terms)} distinct intervention terms: {shown}{more} — "
                "these are different molecules (conjugates, biosimilars, tracers); narrow the "
                "name if you mean one.",
            )
        )
    withdrawn = withdrawn_count(data.records)
    if withdrawn:
        messages.append(
            (
                NoticeCode.WITHDRAWN,
                f"{withdrawn} withdrawn studies (never enrolled) are included in these counts; "
                "add a status filter to exclude them.",
            )
        )
    return messages


def counted_sample_warnings(data: CohortData) -> list[tuple[NoticeCode, str]]:
    """Unreadable sample records on the count route: still counted, only not cited."""
    if not data.skipped:
        return []
    return [
        (
            NoticeCode.SKIPPED_RECORDS,
            f"{len(data.skipped)} of {data.retrieved} sampled citation records could not be "
            "read; the API's bucket counts still include them, but they are not cited.",
        ),
        *skipped_record_lines(data.skipped),
    ]


def retrieval_assumptions(
    data: CohortData,
    plan: QueryPlan,
    cohort_count: int,
    client: CtGovClient,
    policy: CountRoutePolicy,
) -> list[Notice]:
    """State the retrieval route, which records a truncated fetch holds, why it was not
    counted per bucket, and when results came from the cache."""
    if data.retrieval.count is not None:
        messages = counted_assumptions(data, data.retrieval.count, plan, client, policy)
        return [cohort_notice(data, cohort_count, code, text) for code, text in messages]
    fetch = data.fetch
    if fetch is None:
        raise ValueError("A cohort without counts must hold fetched records.")
    messages: list[tuple[NoticeCode, str]] = []
    if fetch.truncated:
        messages.append(
            (
                NoticeCode.RECORD_CAP,
                f"The retrieved trials are the first {data.retrieved:,} of "
                f"{fetch.total_count:,} matches ordered by {RETRIEVAL_SORT_FIELD} "
                f"{RETRIEVAL_SORT_DIRECTION} (the most recently registered), not a random sample; "
                "trials sharing the cutoff date are picked in the API's unspecified tie order; "
                "counts for the remaining matches are unknown and may be distributed differently.",
            )
        )
        if plan.relation is not None:
            messages.append(
                (
                    NoticeCode.RECORD_CAP,
                    "Networks are built from fetched records, so the record cap applies to them.",
                )
            )
        elif plan.dimension is not None and DIMENSIONS[plan.dimension].api_count is None:
            messages.append(
                (
                    NoticeCode.RECORD_CAP,
                    f"The {DIMENSIONS[plan.dimension].label} cannot be counted per bucket by "
                    "the API (its values are not expressible as a fixed set of API filters), "
                    "so the record cap applies.",
                )
            )
    if fetch.cached_at is not None:
        stored = fetch.cached_at.strftime("%Y-%m-%d %H:%M UTC")
        messages.append((NoticeCode.CACHE, f"Served from the local cache stored at {stored}."))
    return [cohort_notice(data, cohort_count, code, text) for code, text in messages]


def counted_assumptions(
    data: CohortData,
    count: CountResult,
    plan: QueryPlan,
    client: CtGovClient,
    policy: CountRoutePolicy,
) -> list[tuple[NoticeCode, str]]:
    """Explain the count_per_bucket route: why it was taken, why it is exact, and that
    citations are a sample of each bucket."""
    if plan.dimension is None:
        raise ValueError("Only a dimension can be counted per bucket.")
    spec = DIMENSIONS[plan.dimension]
    threshold = count_threshold(policy, client)
    limit = (
        f"the record cap ({threshold:,})"
        if policy.above_total is None
        else f"the count-route threshold ({threshold:,})"
    )
    example = count.buckets[0]
    attempt = data.retrieval.attempt
    requests = attempt.requests if attempt is not None else len(count.buckets)
    messages = [
        (
            NoticeCode.RETRIEVAL_ROUTE,
            f"Counts are exact (retrieval_route count_per_bucket): the cohort matched "
            f"{data.total_count:,} trials, more than {limit}, so instead of fetching records the "
            f"API counted each {spec.label} bucket with its own filter ({requests} requests; e.g. "
            f"'{example.key}' = {example.filter_clause}), and the bucket counts add up to the "
            f"API's total of {data.total_count:,}. Citations are a sample: up to the "
            f"{policy.citation_sample_size} most recently registered trials "
            f"({RETRIEVAL_SORT_FIELD} {RETRIEVAL_SORT_DIRECTION}) of each bucket; each bucket's "
            "citation_count_total is its full count.",
        ),
        (
            NoticeCode.RETRIEVAL_ROUTE,
            "meta.cohorts[].drug_matched_terms and withdrawn are null: the API counted each "
            "bucket, so the cohort's records were not read.",
        ),
    ]
    cached = [bucket for bucket in count.buckets if bucket.cached_at is not None]
    if cached:
        oldest = min(bucket.cached_at for bucket in cached if bucket.cached_at is not None)
        messages.append(
            (
                NoticeCode.CACHE,
                f"{len(cached)} of {len(count.buckets)} bucket counts were served from the "
                f"local cache (oldest stored at {oldest.strftime('%Y-%m-%d %H:%M UTC')}).",
            )
        )
    return messages
