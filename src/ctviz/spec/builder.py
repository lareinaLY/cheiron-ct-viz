"""Aggregation results -> VisualizationResponse (chart spec plus meta).

Pure and deterministic: the same inputs always give the same JSON, so the output can be
checked against golden snapshots. Titles are derived from the plan, never from data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from ctviz.aggregate.group import cap_citations
from ctviz.aggregate.registry import DIMENSIONS, RELATIONS, DimensionKind
from ctviz.schemas.aggregate import Graph, GroupedCohorts
from ctviz.schemas.citation import CitationRole
from ctviz.schemas.plan import ChartType, Filters, QueryPlan
from ctviz.schemas.request import cohort_label, count_scope, study_types_label
from ctviz.schemas.response import (
    Channel,
    CohortPairOverlap,
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
    Notice,
    NoticeCode,
    NoticeSeverity,
    RetrievalRoute,
    Visualization,
    VisualizationResponse,
)
from ctviz.schemas.trial import UNKNOWN_BUCKET_KEY
from ctviz.spec.chart import select_chart

# Titles and axes name the counting unit (meta.count_unit): registered studies.
COUNT_CHANNEL = Channel(field="count", type=FieldType.QUANTITATIVE, title="Studies")
TITLE_SUBJECT = "Studies"

NETWORK_SIZE_NOTE = (
    "Node size is the number of trials an entity appears in; a trial counts toward every "
    "entity it lists, so node sizes can add up to more than the number of trials."
)
UNKNOWN_ON_TIME_AXIS_NOTE = (
    "The 'Unknown' bucket has no position on a time axis; it is included in data with "
    "is_unknown=true and should be shown separately."
)
# Shared trials at or above this share of the smaller cohort make a comparison misleading
# enough to warn rather than note: most of one bar is also the other bar.
HEAVY_OVERLAP_SHARE = 0.5


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
    warnings: Sequence[Notice] = (),
    assumptions: Sequence[Notice] = (),
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
    - A response larger than `max_response_bytes` keeps every edge and bar but cuts each
      one's citations to `reduced_edge_citation_cap` (see `fit_network_budget` and
      `fit_grouped_budget`).
    """
    result_warnings: list[Notice]
    if isinstance(result, Graph):
        visualization = network_visualization(plan, result)
        result_assumptions = [
            *result.assumptions,
            Notice.of(NoticeCode.NETWORK_NOTE, NETWORK_SIZE_NOTE),
        ]
        result_warnings = list(result.warnings)
        buckets_overlap = True
        top_n = None
        overlap = None
        estimated_count = None
        unknown_date_type_count = None
    else:
        visualization = grouped_visualization(plan, result)
        result_assumptions = [
            *(a for cohort in result.cohorts for a in cohort.result.assumptions),
            *result.assumptions,
        ]
        if visualization.type == ChartType.TIME_SERIES and any(
            datum.is_unknown for datum in visualization.data
        ):
            result_assumptions.append(
                Notice.of(NoticeCode.UNKNOWN_OFF_AXIS, UNKNOWN_ON_TIME_AXIS_NOTE)
            )
        result_warnings = [
            *cohort_warnings(result),
            *result.warnings,
            *overlap_warnings(plan, result, cohort_stats),
        ]
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
        count_unit=CountUnit.REGISTERED_STUDIES,
        count_scope=count_scope(plan.cohorts),
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
        assumptions=unique_notices([*assumptions, *result_assumptions]),
        warnings=unique_notices([*warnings, *result_warnings]),
        timing_ms=timing_ms,
    )
    response = VisualizationResponse(visualization=visualization, meta=meta)
    if isinstance(result, Graph) and result.summary is not None:
        meta.network = result.summary.model_copy()
        if max_response_bytes is not None:
            fit_network_budget(response, max_response_bytes, reduced_edge_citation_cap)
    elif not isinstance(result, Graph) and max_response_bytes is not None:
        fit_grouped_budget(response, max_response_bytes, reduced_edge_citation_cap)
    return response


def overlap_assumptions(
    result: GroupedCohorts, cohort_stats: Sequence[CohortStats]
) -> list[Notice]:
    """State that compared cohorts share trials, or why the overlap is unknown.

    A grouped bar chart reads as a comparison of disjoint groups, but a trial matching
    several cohorts (e.g. a combination trial) is counted in each.
    """
    if len(result.cohorts) < 2:
        return []
    overlap = result.cohorts_overlap
    if overlap is None:
        return [
            Notice.of(
                NoticeCode.COHORTS_OVERLAP_UNKNOWN,
                "meta.cohorts_overlap is null: at least one cohort was counted per bucket by "
                "the API (count_per_bucket route), which returns only sample trials, so trials "
                "shared between cohorts could not be identified; cohorts may not be disjoint.",
            )
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
    return [Notice.of(NoticeCode.COHORTS_OVERLAP, line)]


def overlap_warnings(
    plan: QueryPlan, result: GroupedCohorts, cohort_stats: Sequence[CohortStats]
) -> list[Notice]:
    """Warn when a pair of cohorts shares most of the smaller one, or all of it.

    Then the bars are mostly the same studies, so they do not compare separate groups;
    the warning names the share and the filter behaviour that most likely causes it.
    Sizes are the records compared (retrieved minus skipped), the set the overlap was
    computed on.
    """
    if result.cohorts_overlap is None:
        return []
    sizes = {stats.label: stats.records_retrieved - stats.records_skipped for stats in cohort_stats}
    filters = {cohort.label: cohort.filters for cohort in plan.cohorts}
    return [
        warning
        for pair in result.cohorts_overlap.pairs
        if (warning := pair_overlap_warning(pair, sizes, filters)) is not None
    ]


def pair_overlap_warning(
    pair: CohortPairOverlap, sizes: dict[str, int], filters: dict[str, Filters]
) -> Notice | None:
    """The heavy-overlap or subset warning for one pair, or None below HEAVY_OVERLAP_SHARE."""
    first, second = pair.cohorts
    small, large = sorted((first, second), key=lambda label: (sizes.get(label, 0), label))
    small_size = sizes.get(small, 0)
    shared = pair.shared_trials
    if small_size == 0 or shared / small_size < HEAVY_OVERLAP_SHARE:
        return None
    cause = overlap_cause(filters[first], filters[second])
    if shared >= small_size:
        return Notice.of(
            NoticeCode.COHORT_SUBSET,
            f"{small} is a subset of {large}: all {small_size} {small} studies are also counted "
            f"under {large}, so the bars are not a comparison of separate groups. Likely "
            f"cause: {cause}.",
        )
    return Notice.of(
        NoticeCode.COHORTS_OVERLAP,
        f"{small} and {large} share {shared} studies: {shared} of the {small_size} {small} "
        f"studies ({shared / small_size:.0%}) are also counted under {large}, so the bars are "
        f"not a comparison of separate groups. Likely cause: {cause}.",
        severity=NoticeSeverity.WARNING,
    )


def overlap_cause(first: Filters, second: Filters) -> str:
    """The filter behaviour that most likely puts the same studies in both cohorts."""
    if first.condition and second.condition and first.condition != second.condition:
        inner, outer = contained(first.condition, second.condition)
        if inner is not None:
            return f"the term search for '{inner}' also matches '{outer}'"
        return (
            "condition search is a term search with synonyms, so one condition can match "
            "inside another's name (e.g. 'small cell lung cancer' inside 'non-small cell "
            "lung cancer')"
        )
    if first.drug and second.drug and first.drug != second.drug:
        inner, outer = contained(first.drug, second.drug)
        example = (
            f"'{inner}' also matches '{outer}'"
            if inner is not None
            else "one drug's name can occur inside the other's (e.g. 'trastuzumab' inside "
            "'trastuzumab deruxtecan')"
        )
        return f"drug names match by 'contains' on intervention names and MeSH terms, so {example}"
    return "the cohorts' filters are not mutually exclusive, so a study can satisfy both"


def contained(first: str, second: str) -> tuple[str, str] | tuple[None, None]:
    """(shorter, longer) when one value occurs inside the other (case-insensitive)."""
    a, b = first.strip(), second.strip()
    if a.casefold() in b.casefold():
        return a, b
    if b.casefold() in a.casefold():
        return b, a
    return None, None


def fit_network_budget(
    response: VisualizationResponse, max_response_bytes: int, reduced_edge_citation_cap: int
) -> None:
    """Cut citations (never nodes or edges) when the serialized response is over budget.

    First each edge keeps `reduced_edge_citation_cap` citations. If the response is still
    over budget, nodes drop their supporting citations (the raw intervention names listed
    next to a MeSH term, which umbrella trials make long) and keep every primary one.
    Dropping nodes or edges would change the graph; `cited_trials_total` and
    `citation_count_total` keep the true numbers. Each reduction is stated in meta.
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
        f"{max_response_bytes / 1_000_000:.1f} MB size limit, so citations per link were cut "
        f"to {summary.edge_citation_cap}; no link was dropped, and each link still reports "
        "its full number of studies."
    )
    if reduced > max_response_bytes:
        for element in response.visualization.nodes:
            element.data.citations = [
                c for c in element.data.citations if c.role == CitationRole.PRIMARY
            ]
        summary.node_supporting_citations_dropped = True
        reduced = len(response.model_dump_json().encode())
        message += (
            " Still over the limit, so the supporting citations of nodes were dropped (each node "
            "keeps its primary citations)."
        )
    message += f" Size now {reduced / 1_000_000:.2f} MB."
    if reduced > max_response_bytes:
        message += " The response is still over the limit."
    response.meta.warnings.append(Notice.of(NoticeCode.CITATIONS_REDUCED, message))


def fit_grouped_budget(
    response: VisualizationResponse, max_response_bytes: int, reduced_citation_cap: int
) -> None:
    """Cut per-datum citations (never data) when the serialized response is over budget.

    The same budget as networks: an open-vocabulary dimension can produce thousands of
    bars with 20 citations each. Every bar stays, each keeps `reduced_citation_cap`
    primary citations, and `cited_trials_total` still gives the true number.
    """
    size = len(response.model_dump_json().encode())
    if size <= max_response_bytes:
        return
    for datum in response.visualization.data:
        datum.citations = cap_citations(datum.citations, reduced_citation_cap)
    reduced = len(response.model_dump_json().encode())
    message = (
        f"The response was {size / 1_000_000:.1f} MB, over the "
        f"{max_response_bytes / 1_000_000:.1f} MB size limit, so citations per bar were cut "
        f"to {reduced_citation_cap}; no bar was dropped, and each bar still reports its full "
        f"number of studies. Size now {reduced / 1_000_000:.2f} MB."
    )
    if reduced > max_response_bytes:
        message += " The response is still over the limit."
    response.meta.warnings.append(Notice.of(NoticeCode.CITATIONS_REDUCED, message))


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
    """Deterministic title from the plan, e.g. 'Studies by phase for pembrolizumab'."""
    labels = [cohort.label for cohort in plan.cohorts]
    if plan.relation is not None:
        return RELATIONS[plan.relation].title.format(cohort=labels[0])
    if plan.dimension is None:
        raise ValueError("A validated plan has a dimension or a relation.")
    shared_types = shared_study_types_label(plan)
    if shared_types is not None:
        # The subject names the study types, so the labels need not repeat them.
        subject = f"{capitalize(shared_types)} {TITLE_SUBJECT.lower()}"
        labels = [cohort_label(c.filters, with_study_types=False) for c in plan.cohorts]
    else:
        subject = TITLE_SUBJECT
    subject = f"{subject} by {DIMENSIONS[plan.dimension].title_label}"
    title = (
        f"{subject} for {labels[0]}" if len(labels) == 1 else f"{subject}: {' vs '.join(labels)}"
    )
    if plan.top_n is not None:
        title += f" (top {plan.top_n})"
    if plan.bottom_n is not None:
        title += f" (bottom {plan.bottom_n})"
    return title


def shared_study_types_label(plan: QueryPlan) -> str | None:
    """The study-type label all cohorts share ('interventional'), else None."""
    labels = {study_types_label(cohort.filters) for cohort in plan.cohorts}
    if len(labels) != 1:
        return None
    return labels.pop()


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
            is_not_applicable=bucket.is_not_applicable,
            is_other=bucket.is_other,
            citations=bucket.citations,
            cited_trials_total=bucket.cited_trials_total,
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
    title = chart_title(plan)
    if plan.top_n is None and plan.bottom_n is None and result.top_n is not None:
        # The readability cap chose top_n; the title says so like a requested one.
        title += f" (top {result.top_n.top_n})"
    return Visualization(
        type=chart_type,
        title=title,
        encoding=Encoding(x=x_channel(result, chart_type, data), y=COUNT_CHANNEL, color=color),
        data=data,
    )


def x_channel(result: GroupedCohorts, chart_type: ChartType, data: list[Datum]) -> Channel:
    """The dimension axis: temporal for time series, otherwise nominal in key order.

    A start-date axis lists every period from the earliest to the latest one present in
    any cohort (`period_domain`), so a renderer draws periods with no studies as zero
    instead of skipping them. After a top/bottom-N limit it lists only the shown periods:
    an omitted period's count is not zero, only not shown.
    """
    spec = DIMENSIONS[result.dimension]
    title = capitalize(spec.title_label)
    domain = None
    if spec.kind == DimensionKind.TEMPORAL:
        domain = (
            period_domain(data, spec.time_unit)
            if result.top_n is None
            else unique(datum.key for datum in data)
        )
    if chart_type == ChartType.TIME_SERIES and spec.kind == DimensionKind.TEMPORAL:
        return Channel(
            field="key", type=FieldType.TEMPORAL, title=title, sort=domain, time_unit=spec.time_unit
        )
    labels = domain if domain is not None else unique(datum.label for datum in data)
    return Channel(field="label", type=FieldType.NOMINAL, title=title, sort=labels)


def period_domain(data: list[Datum], time_unit: str | None) -> list[str] | None:
    """Every period key from the first to the last present, then Unknown if present.

    Keys are 'YYYY' or 'YYYY-MM'; the range is integer arithmetic on them, no dates.
    Temporal labels equal their keys, so the list serves the key and the label axis.
    """
    periods = sorted({d.key for d in data if not (d.is_unknown or d.is_unrecognized)})
    if not periods:
        return None
    if time_unit == "yearmonth":
        domain = month_range(periods[0], periods[-1])
    else:
        domain = [str(year) for year in range(int(periods[0]), int(periods[-1]) + 1)]
    flagged = unique(d.key for d in data if d.is_unrecognized or d.is_unknown)
    return [
        *domain,
        *(key for key in flagged if key != UNKNOWN_BUCKET_KEY),
        *([UNKNOWN_BUCKET_KEY] if UNKNOWN_BUCKET_KEY in flagged else []),
    ]


def month_range(first: str, last: str) -> list[str]:
    """'2025-11'..'2026-02' -> ['2025-11', '2025-12', '2026-01', '2026-02']."""
    year, month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    months: list[str] = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def network_visualization(plan: QueryPlan, graph: Graph) -> Visualization:
    """Cytoscape-style elements; edge ids are '<source>--<target>'."""
    return Visualization(
        type=select_chart(plan, graph),
        title=chart_title(plan),
        encoding=Encoding(
            color=Channel(field="type", type=FieldType.NOMINAL, title="Entity type"),
            size=Channel(field="size", type=FieldType.QUANTITATIVE, title=TITLE_SUBJECT),
        ),
        nodes=[NodeElement(data=node) for node in graph.nodes],
        edges=[
            EdgeElement(data=EdgeData(id=f"{edge.source}--{edge.target}", **edge.model_dump()))
            for edge in graph.edges
        ],
    )


def cohort_warnings(result: GroupedCohorts) -> list[Notice]:
    """Aggregate warnings, naming their cohort; the message is also prefixed with the
    cohort label when there are several cohorts."""
    several = len(result.cohorts) > 1
    return [
        warning.model_copy(
            update={
                "cohort": cohort.label,
                "message": f"{cohort.label}: {warning.message}" if several else warning.message,
            }
        )
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


def unique_notices(notices: Iterable[Notice]) -> list[Notice]:
    """Notices in first-seen order without repeats (same code, cohort and message)."""
    seen: dict[tuple[str, str | None, str], Notice] = {}
    for notice in notices:
        seen.setdefault((notice.code, notice.cohort, notice.message), notice)
    return list(seen.values())


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
