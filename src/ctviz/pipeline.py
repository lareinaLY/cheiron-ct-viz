"""The fixed pipeline: validate -> retrieve -> normalize -> aggregate -> spec.

The planner runs before it (see `api.app`) and hands over an `Interpretation`. Each
stage is called once per request and timed. Facts that the chart alone cannot show
(how the plan was made, explicit overrides, truncation, reconciliation, duplicates,
skipped and unrecognized records, data quality, cache use) become meta warnings or
assumptions here (invariants 3 and 6).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from ctviz.aggregate import build_network, group_cohorts
from ctviz.config import DataQualityThresholds, NetworkLimits
from ctviz.errors import (
    RateLimitError,
    SourceRecordError,
    UpstreamDataFormatError,
    UpstreamError,
)
from ctviz.normalize import normalize_study, unrecognized_warnings
from ctviz.planner import validate_plan
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.query_builder import build_params, search_semantics
from ctviz.schemas.aggregate import Graph, GroupedCohorts
from ctviz.schemas.plan import Cohort, QueryPlan
from ctviz.schemas.response import (
    CohortStats,
    DataQuality,
    Interpretation,
    VisualizationResponse,
)
from ctviz.schemas.retrieval import FetchResult
from ctviz.schemas.trial import TrialRecord
from ctviz.spec import build_response

logger = logging.getLogger(__name__)


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
    """One cohort's fetch and the records that survived normalization."""

    cohort: Cohort
    fetch: FetchResult
    records: list[TrialRecord]
    skipped: list[SourceRecordError]

    @property
    def retrieved(self) -> int:
        """Records the API returned for this cohort (after deduplication)."""
        return len(self.fetch.studies)


async def run(
    plan: QueryPlan,
    *,
    question: str,
    client: CtGovClient,
    as_of: date | None = None,
    network_limits: NetworkLimits | None = None,
    data_quality_thresholds: DataQualityThresholds | None = None,
) -> VisualizationResponse:
    """Answer a plan supplied directly by code (no planner; planned_by "structured_fields")."""
    interpretation = Interpretation(question=question, plan=plan, planned_by="structured_fields")
    return await execute(
        interpretation,
        client=client,
        as_of=as_of,
        network_limits=network_limits,
        data_quality_thresholds=data_quality_thresholds,
    )


async def execute(
    interpretation: Interpretation,
    *,
    client: CtGovClient,
    as_of: date | None = None,
    network_limits: NetworkLimits | None = None,
    data_quality_thresholds: DataQualityThresholds | None = None,
) -> VisualizationResponse:
    """Answer the planner's plan with live data.

    Raises PlanValidationError or InvalidFilterError before any network call, and
    RetrievalError subclasses if /studies cannot be fetched; a failing /version only
    nulls `meta.data_timestamp`, with a warning naming the cause. A record that fails to
    normalize is skipped and reported, never fatal by itself; a record with an
    unrecognized value is kept. When a cohort's share of skipped records reaches the
    refuse threshold, UpstreamDataFormatError is raised instead of returning a chart;
    at the degraded threshold the response is marked `data_quality="degraded"`.
    `network_limits` and `data_quality_thresholds` default to the configured defaults.
    """
    plan, question = interpretation.plan, interpretation.question
    limits = network_limits or NetworkLimits()
    thresholds = data_quality_thresholds or DataQualityThresholds()
    timer = StageTimer()
    started = time.perf_counter()

    with timer.stage("validate"):
        validate_plan(plan)
        cohort_params = [build_params(c.filters, plan.match_mode) for c in plan.cohorts]

    with timer.stage("retrieve"):
        fetches = [await client.fetch_all(params) for params in cohort_params]
        data_timestamp, timestamp_problem = await fetch_data_timestamp(client)

    with timer.stage("normalize"):
        cohorts = [
            CohortData(cohort, fetch, *normalize_all(fetch.studies))
            for cohort, fetch in zip(plan.cohorts, fetches, strict=True)
        ]
        data_quality = assess_data_quality(cohorts, thresholds)

    with timer.stage("aggregate"):
        result = aggregate(plan, cohorts, as_of, limits)

    with timer.stage("spec"):
        response = build_response(
            plan,
            result,
            question=question,
            cohort_stats=[cohort_stats(data) for data in cohorts],
            search_semantics=merged_search_semantics(plan),
            data_timestamp=data_timestamp,
            timing_ms={},
            warnings=[
                *data_quality_warnings(cohorts, data_quality, thresholds),
                *data_timestamp_warnings(timestamp_problem),
                *(w for data in cohorts for w in retrieval_warnings(data, len(cohorts))),
            ],
            assumptions=[
                *planning_assumptions(interpretation),
                *ranking_assumptions(interpretation, result),
                *(a for data in cohorts for a in retrieval_assumptions(data, len(cohorts))),
            ],
            interpretation=interpretation,
            max_response_bytes=limits.max_response_bytes,
            reduced_edge_citation_cap=limits.reduced_edge_citation_cap,
        )

    timer.timings["total"] = round((time.perf_counter() - started) * 1000, 1)
    response.meta.timing_ms = timer.timings
    response.meta.data_quality = data_quality
    return response


def planning_assumptions(interpretation: Interpretation) -> list[str]:
    """How the plan was made: who read the question, and what explicit fields replaced."""
    if interpretation.planned_by == "rules":
        return [
            "The question text was not interpreted (no LLM planner is configured); the "
            "request's structured fields alone define the query."
        ]
    if interpretation.planned_by == "structured_fields":
        return ["The question text was not interpreted; the plan was supplied directly."]
    messages = [
        f"The plan was drafted from the question by the LLM ({interpretation.model_used} via "
        f"{interpretation.provider}) and validated; meta.interpretation.draft holds the "
        "draft. Counts come from the registry."
    ]
    if interpretation.fallback_reason is not None:
        messages.append(
            f"The primary LLM provider did not answer, so a fallback provider "
            f"({interpretation.provider}) drafted the plan: {interpretation.fallback_reason}"
        )
    messages.extend(
        f"The explicit request field set `{override.path}` to "
        f"{json.dumps(override.explicit_value)}; the LLM had read "
        f"{json.dumps(override.llm_value)} from the question."
        for override in interpretation.overrides
    )
    messages.extend(
        f"An earlier LLM draft was rejected and redrafted: {problem}"
        for problem in interpretation.rejected_attempts
    )
    return messages


def ranking_assumptions(
    interpretation: Interpretation, result: GroupedCohorts | Graph
) -> list[str]:
    """State that the planner chose top_n because the question asked for a ranking."""
    top_n = interpretation.ranking_top_n
    if top_n is None or not isinstance(result, GroupedCohorts) or result.top_n is None:
        return []
    omitted = len(result.top_n.omitted_keys)
    return [
        f"Question asked for a ranking; showing top {top_n} (configurable); "
        f"{omitted} buckets omitted"
    ]


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
) -> list[str]:
    """The prominent warning for a degraded result, placed first in meta.warnings."""
    if quality != DataQuality.DEGRADED:
        return []
    degraded = [
        data
        for data in cohorts
        if classify_skip_rate(data.retrieved, len(data.skipped), thresholds) == SkipBand.DEGRADED
    ]
    return [
        prefixed(
            data,
            len(cohorts),
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
    plan: QueryPlan, cohorts: Sequence[CohortData], as_of: date | None, limits: NetworkLimits
) -> GroupedCohorts | Graph:
    """Dispatch to the network builder or the per-cohort grouping."""
    if plan.relation is not None:
        return build_network(
            cohorts[0].records,
            plan.relation,
            max_nodes=limits.max_nodes,
            min_edge_weight=limits.min_edge_weight,
        )
    if plan.dimension is None:
        raise ValueError("A validated plan has a dimension or a relation.")
    return group_cohorts(
        [(data.cohort.label, data.records) for data in cohorts],
        plan.dimension,
        top_n=plan.top_n,
        as_of=as_of,
    )


def cohort_stats(data: CohortData) -> CohortStats:
    """Retrieval accounting for one cohort."""
    return CohortStats(
        label=data.cohort.label,
        total_matched=data.fetch.total_count,
        records_retrieved=data.retrieved,
        records_skipped=len(data.skipped),
        truncated=data.fetch.truncated,
        coverage=data.fetch.coverage,
        reconciled=data.fetch.reconciled,
    )


def merged_search_semantics(plan: QueryPlan) -> dict[str, str]:
    """Search semantics of every filter used by any cohort."""
    semantics: dict[str, str] = {}
    for cohort in plan.cohorts:
        semantics.update(search_semantics(cohort.filters, plan.match_mode))
    return semantics


def prefixed(data: CohortData, cohort_count: int, message: str) -> str:
    """Name the cohort when several cohorts share one meta block."""
    return f"{data.cohort.label}: {message}" if cohort_count > 1 else message


def retrieval_warnings(data: CohortData, cohort_count: int) -> list[str]:
    """Empty results, truncation, reconciliation failures, duplicates, skipped records and
    unrecognized values."""
    fetch = data.fetch
    retrieved = data.retrieved
    messages: list[str] = []
    if fetch.total_count == 0:
        messages.append("No trials matched these filters.")
    if fetch.truncated and fetch.coverage is not None:
        messages.append(
            f"Only the first {retrieved} of {fetch.total_count} matching trials were "
            f"retrieved (record cap); counts cover {fetch.coverage:.1%} of matches."
        )
    if not fetch.reconciled:
        messages.append(
            f"Retrieved {retrieved} trials but the API reported {fetch.total_count} matches; "
            "the data may have changed during pagination, so counts can be off by the "
            "difference."
        )
    if fetch.duplicates_dropped:
        messages.append(
            f"{fetch.duplicates_dropped} duplicate records (same NCT ID on several pages) "
            "were dropped."
        )
    if data.skipped:
        messages.append(
            f"{len(data.skipped)} of {retrieved} retrieved trials were skipped because the "
            "record could not be read; they are not counted."
        )
        messages.extend(f"Skipped {error}" for error in data.skipped)
    messages.extend(unrecognized_warnings(data.records))
    return [prefixed(data, cohort_count, message) for message in messages]


def retrieval_assumptions(data: CohortData, cohort_count: int) -> list[str]:
    """State when results came from the local cache instead of a live fetch."""
    if data.fetch.cached_at is None:
        return []
    stored = data.fetch.cached_at.strftime("%Y-%m-%d %H:%M UTC")
    return [prefixed(data, cohort_count, f"Served from the local cache stored at {stored}.")]
