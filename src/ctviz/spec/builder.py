"""Aggregation results -> VisualizationResponse (chart spec plus meta).

Pure and deterministic: the same inputs always give the same JSON, so the output can be
checked against golden snapshots. Titles are derived from the plan, never from data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from ctviz.aggregate.registry import DIMENSIONS, RELATIONS, DimensionKind
from ctviz.schemas.aggregate import Graph, GroupedCohorts
from ctviz.schemas.plan import ChartType, QueryPlan
from ctviz.schemas.response import (
    Channel,
    CohortStats,
    CountUnit,
    Datum,
    EdgeData,
    EdgeElement,
    Encoding,
    FieldType,
    Interpretation,
    Meta,
    NodeElement,
    RetrievalRoute,
    Visualization,
    VisualizationResponse,
)
from ctviz.spec.chart import select_chart

COUNT_CHANNEL = Channel(field="count", type=FieldType.QUANTITATIVE, title="Trials")

NETWORK_SIZE_NOTE = (
    "Node size is the number of trials an entity appears in; a trial counts toward every "
    "entity it lists, so node sizes can add up to more than the number of trials."
)
UNKNOWN_ON_TIME_AXIS_NOTE = (
    "The 'Unknown' bucket has no position on a time axis; it is included in data with "
    "is_unknown=true and should be shown separately."
)


def build_response(
    plan: QueryPlan,
    result: GroupedCohorts | Graph,
    *,
    question: str,
    cohort_stats: Sequence[CohortStats],
    retrieval_route: RetrievalRoute = RetrievalRoute.FETCH,
    search_semantics: dict[str, str],
    data_timestamp: datetime | None,
    timing_ms: dict[str, float],
    warnings: Sequence[str] = (),
    assumptions: Sequence[str] = (),
    interpretation: Interpretation | None = None,
    max_response_bytes: int | None = None,
    reduced_edge_citation_cap: int = 2,
) -> VisualizationResponse:
    """Build the chart spec and its meta block.

    `interpretation` comes from the planner; without one, the plan is recorded as
    passed in directly (planned_by "structured_fields").

    - Grouped results become `data` (one Datum per cohort and bucket, in the shared key
      order) with Vega-Lite-style encoding; several cohorts are colored by `cohort`.
    - Graphs become Cytoscape-style `nodes`/`edges` elements.
    - When the record cap cut a cohort's fetch, the title ends with
      " (partial: first N of M matches)".
    - Meta carries the count unit, the retrieval route, retrieval totals summed over
      cohorts (coverage weighted by each cohort's matches), per-cohort stats, and every
      assumption and warning (aggregate ones after the caller's).
    - A network response larger than `max_response_bytes` keeps every edge but cuts each
      edge's citations to `reduced_edge_citation_cap` (see `fit_network_budget`).
    """
    if isinstance(result, Graph):
        visualization = network_visualization(plan, result)
        result_assumptions = [*result.assumptions, NETWORK_SIZE_NOTE]
        result_warnings = list(result.warnings)
        buckets_overlap = True
        top_n = None
        overlap = None
        estimated_count = None
        unknown_date_type_count = None
    else:
        visualization = grouped_visualization(plan, result)
        result_assumptions = [a for cohort in result.cohorts for a in cohort.result.assumptions]
        if visualization.type == ChartType.TIME_SERIES and any(
            datum.is_unknown for datum in visualization.data
        ):
            result_assumptions.append(UNKNOWN_ON_TIME_AXIS_NOTE)
        result_warnings = cohort_warnings(result)
        buckets_overlap = any(cohort.result.buckets_overlap for cohort in result.cohorts)
        top_n = result.top_n
        overlap = result.cohorts_overlap
        result_assumptions.extend(overlap_assumptions(result, cohort_stats))
        estimated_count = summed_if_known(
            [cohort.result.estimated_count for cohort in result.cohorts]
        )
        unknown_date_type_count = summed_if_known(
            [cohort.result.unknown_date_type_count for cohort in result.cohorts]
        )

    visualization.title += partial_title_suffix(cohort_stats)
    total_matched = sum(stats.total_matched for stats in cohort_stats) if cohort_stats else None
    records_retrieved = sum(stats.records_retrieved for stats in cohort_stats)
    covered = sum((stats.coverage or 0.0) * stats.total_matched for stats in cohort_stats)
    meta = Meta(
        interpretation=resolve_interpretation(plan, question, interpretation),
        count_unit=CountUnit.TRIALS,
        filters_applied=plan.cohorts,
        match_mode=plan.match_mode,
        search_semantics=search_semantics,
        buckets_overlap=buckets_overlap,
        total_matched=total_matched,
        records_retrieved=records_retrieved,
        truncated=any(stats.truncated for stats in cohort_stats),
        coverage=covered / total_matched if total_matched else None,
        retrieval_route=retrieval_route,
        cohorts=list(cohort_stats),
        top_n=top_n,
        cohorts_overlap=overlap,
        estimated_count=estimated_count,
        unknown_date_type_count=unknown_date_type_count,
        data_timestamp=data_timestamp,
        assumptions=unique([*assumptions, *result_assumptions]),
        warnings=unique([*warnings, *result_warnings]),
        timing_ms=timing_ms,
    )
    response = VisualizationResponse(visualization=visualization, meta=meta)
    if isinstance(result, Graph) and result.summary is not None:
        meta.network = result.summary.model_copy()
        if max_response_bytes is not None:
            fit_network_budget(response, max_response_bytes, reduced_edge_citation_cap)
    return response


def overlap_assumptions(result: GroupedCohorts, cohort_stats: Sequence[CohortStats]) -> list[str]:
    """State that compared cohorts share trials, or why the overlap is unknown.

    A grouped bar chart reads as a comparison of disjoint groups, but a trial matching
    several cohorts (e.g. a combination trial) is counted in each.
    """
    if len(result.cohorts) < 2:
        return []
    overlap = result.cohorts_overlap
    if overlap is None:
        return [
            "meta.cohorts_overlap is null: at least one cohort was counted per bucket by "
            "the API (count_per_bucket route), which returns only sample trials, so trials "
            "shared between cohorts could not be identified; cohorts may not be disjoint."
        ]
    if overlap.total_shared == 0:
        return []
    line = (
        f"{overlap.total_shared} trials match more than one cohort and are counted in each; "
        "cohorts are not disjoint."
    )
    if any(stats.truncated for stats in cohort_stats):
        line += (
            " The overlap is computed over the retrieved records only; the record cap cut "
            "at least one cohort, so more trials may be shared."
        )
    return [line]


def fit_network_budget(
    response: VisualizationResponse, max_response_bytes: int, reduced_edge_citation_cap: int
) -> None:
    """Cut edge citations (never edges) when the serialized response is over budget.

    Edges carry most of a large network's bytes; dropping edges would change the graph,
    while fewer citations per edge keep every edge cited and `citation_count_total`
    still gives the true number. The reduction is stated in meta.
    """
    size = len(response.model_dump_json().encode())
    summary = response.meta.network
    if size <= max_response_bytes or summary is None:
        return
    for element in response.visualization.edges:
        element.data.citations = element.data.citations[:reduced_edge_citation_cap]
    summary.edge_citation_cap = min(summary.edge_citation_cap, reduced_edge_citation_cap)
    summary.edge_citations_reduced = True
    summary.response_bytes_before_reduction = size
    reduced = len(response.model_dump_json().encode())
    message = (
        f"The network response was {size / 1_000_000:.1f} MB, over the "
        f"{max_response_bytes / 1_000_000:.1f} MB budget (NETWORK_MAX_RESPONSE_BYTES), so "
        f"citations per edge were cut to {summary.edge_citation_cap} "
        f"(NETWORK_REDUCED_EDGE_CITATION_CAP); no edge was dropped and each edge's "
        f"citation_count_total keeps the true number. Size now {reduced / 1_000_000:.2f} MB."
    )
    if reduced > max_response_bytes:
        message += " The response is still over budget."
    response.meta.warnings.append(message)


def partial_title_suffix(cohort_stats: Sequence[CohortStats]) -> str:
    """' (partial: first 10,000 of 16,913 matches)' when the record cap cut a fetch, else ''.

    A chart over part of the matches must say so where readers look first; with several
    cohorts, only the truncated ones are named.
    """
    truncated = [stats for stats in cohort_stats if stats.truncated]
    if not truncated:
        return ""
    parts = [
        f"first {stats.records_retrieved:,} of {stats.total_matched:,} matches"
        for stats in truncated
    ]
    if len(cohort_stats) > 1:
        parts = [f"{stats.label} {part}" for stats, part in zip(truncated, parts, strict=True)]
    return f" (partial: {'; '.join(parts)})"


def chart_title(plan: QueryPlan) -> str:
    """Deterministic title from the plan, e.g. 'Trials by phase for pembrolizumab'."""
    labels = [cohort.label for cohort in plan.cohorts]
    if plan.relation is not None:
        return f"{capitalize(RELATIONS[plan.relation].label)} network for {labels[0]}"
    if plan.dimension is None:
        raise ValueError("A validated plan has a dimension or a relation.")
    subject = f"Trials by {DIMENSIONS[plan.dimension].title_label}"
    title = (
        f"{subject} for {labels[0]}" if len(labels) == 1 else f"{subject}: {' vs '.join(labels)}"
    )
    if plan.top_n is not None:
        title += f" (top {plan.top_n})"
    return title


def grouped_visualization(plan: QueryPlan, result: GroupedCohorts) -> Visualization:
    """Bars or points: one Datum per (bucket, cohort), ordered by the shared key order."""
    chart_type = select_chart(plan, result.cohorts[0].result)
    buckets_by_key = [
        {bucket.key: bucket for bucket in cohort.result.buckets} for cohort in result.cohorts
    ]
    data = [
        Datum(
            key=bucket.key,
            label=bucket.label,
            cohort=cohort.label,
            count=bucket.count,
            estimated_count=bucket.estimated_count,
            unknown_date_type_count=bucket.unknown_date_type_count,
            incomplete=bucket.incomplete,
            is_future=bucket.is_future,
            is_unknown=bucket.is_unknown,
            is_unrecognized=bucket.is_unrecognized,
            citations=bucket.citations,
            citation_count_total=bucket.citation_count_total,
        )
        for key in result.key_order
        for cohort, buckets in zip(result.cohorts, buckets_by_key, strict=True)
        if (bucket := buckets.get(key)) is not None
    ]
    color = None
    if len(result.cohorts) > 1:
        color = Channel(
            field="cohort",
            type=FieldType.NOMINAL,
            title="Cohort",
            sort=[cohort.label for cohort in result.cohorts],
        )
    return Visualization(
        type=chart_type,
        title=chart_title(plan),
        encoding=Encoding(x=x_channel(result, chart_type, data), y=COUNT_CHANNEL, color=color),
        data=data,
    )


def x_channel(result: GroupedCohorts, chart_type: ChartType, data: list[Datum]) -> Channel:
    """The dimension axis: temporal for time series, otherwise nominal in key order."""
    spec = DIMENSIONS[result.dimension]
    title = capitalize(spec.title_label)
    if chart_type == ChartType.TIME_SERIES and spec.kind == DimensionKind.TEMPORAL:
        return Channel(field="key", type=FieldType.TEMPORAL, title=title, time_unit=spec.time_unit)
    labels = unique(datum.label for datum in data)
    return Channel(field="label", type=FieldType.NOMINAL, title=title, sort=labels)


def network_visualization(plan: QueryPlan, graph: Graph) -> Visualization:
    """Cytoscape-style elements; edge ids are '<source>--<target>'."""
    return Visualization(
        type=select_chart(plan, graph),
        title=chart_title(plan),
        encoding=Encoding(
            color=Channel(field="type", type=FieldType.NOMINAL, title="Entity type"),
            size=Channel(field="size", type=FieldType.QUANTITATIVE, title="Trials"),
        ),
        nodes=[NodeElement(data=node) for node in graph.nodes],
        edges=[
            EdgeElement(data=EdgeData(id=f"{edge.source}--{edge.target}", **edge.model_dump()))
            for edge in graph.edges
        ],
    )


def cohort_warnings(result: GroupedCohorts) -> list[str]:
    """Aggregate warnings; prefixed with the cohort label when there are several cohorts."""
    if len(result.cohorts) == 1:
        return list(result.cohorts[0].result.warnings)
    return [
        f"{cohort.label}: {warning}"
        for cohort in result.cohorts
        for warning in cohort.result.warnings
    ]


def summed_if_known(counts: list[int | None]) -> int | None:
    """Sum over cohorts; None when any cohort's count does not apply or is not determined.

    A sum over only some cohorts would present a partial count as the total.
    """
    if any(count is None for count in counts):
        return None
    return sum(count for count in counts if count is not None)


def unique(items: Iterable[str]) -> list[str]:
    """Items in first-seen order without repeats."""
    return list(dict.fromkeys(items))


def capitalize(text: str) -> str:
    """Upper-case the first character only ('lead sponsor' -> 'Lead sponsor')."""
    return text[:1].upper() + text[1:]


def resolve_interpretation(
    plan: QueryPlan, question: str, interpretation: Interpretation | None
) -> Interpretation:
    """The planner's interpretation, checked against the executed plan, or a direct-plan one."""
    if interpretation is None:
        return Interpretation(question=question, plan=plan, planned_by="structured_fields")
    if interpretation.plan != plan or interpretation.question != question:
        raise ValueError("The interpretation does not describe the plan being executed.")
    return interpretation
