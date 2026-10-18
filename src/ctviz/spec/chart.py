"""Deterministic chart-type selection."""

from __future__ import annotations

from ctviz.aggregate.registry import DIMENSIONS, DimensionKind
from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.schemas.aggregate import Graph, GroupResult
from ctviz.schemas.plan import ChartType, QueryPlan

NETWORK_CHARTS = frozenset({ChartType.NETWORK_GRAPH})

# Chart types that can faithfully show each kind of dimension.
CHARTS_BY_KIND: dict[DimensionKind, frozenset[ChartType]] = {
    DimensionKind.CATEGORICAL: frozenset({ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART}),
    DimensionKind.TEMPORAL: frozenset(
        {ChartType.TIME_SERIES, ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART}
    ),
    DimensionKind.BINNED: frozenset(
        {ChartType.HISTOGRAM, ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART}
    ),
}


def allowed_chart_types(plan: QueryPlan) -> frozenset[ChartType]:
    """Chart types compatible with the plan's dimension or relation."""
    if plan.relation is not None:
        return NETWORK_CHARTS
    if plan.dimension is None:
        raise PlanValidationError(
            PlanErrorReason.NO_DIMENSION_OR_RELATION,
            "The plan needs a dimension or a relation to choose a chart.",
        )
    return CHARTS_BY_KIND[DIMENSIONS[plan.dimension].kind]


def default_chart_type(plan: QueryPlan) -> ChartType:
    """The chart used when the plan has no hint."""
    if plan.relation is not None:
        return ChartType.NETWORK_GRAPH
    kind = DIMENSIONS[plan.dimension].kind if plan.dimension else None
    if kind == DimensionKind.TEMPORAL:
        return ChartType.TIME_SERIES
    if kind == DimensionKind.BINNED:
        return ChartType.HISTOGRAM
    return ChartType.GROUPED_BAR_CHART if len(plan.cohorts) > 1 else ChartType.BAR_CHART


def check_chart_hint(plan: QueryPlan) -> None:
    """Raise PlanValidationError if the plan's chart_hint cannot show its data."""
    if plan.chart_hint is None:
        return
    allowed = allowed_chart_types(plan)
    if plan.chart_hint not in allowed:
        subject = f"relation {plan.relation}" if plan.relation else f"dimension {plan.dimension}"
        raise PlanValidationError(
            PlanErrorReason.CHART_HINT_INCOMPATIBLE,
            f"chart_hint {plan.chart_hint} cannot show {subject}; "
            f"use one of: {', '.join(sorted(allowed))}.",
        )


def check_result_matches_plan(plan: QueryPlan, aggregate_result: GroupResult | Graph) -> None:
    """Guard against wiring the wrong aggregation result to a plan."""
    if isinstance(aggregate_result, Graph):
        matches = aggregate_result.relation == plan.relation
        produced = f"relation {aggregate_result.relation}"
    else:
        matches = aggregate_result.dimension == plan.dimension
        produced = f"dimension {aggregate_result.dimension}"
    if not matches:
        expected = plan.relation or plan.dimension
        raise ValueError(f"Plan asks for {expected} but the aggregation produced {produced}.")


def select_chart(plan: QueryPlan, aggregate_result: GroupResult | Graph) -> ChartType:
    """Pick the chart type for a validated plan.

    Defaults: relation -> network_graph; start_year/start_month -> time_series;
    enrollment_bucket -> histogram; other dimensions -> bar_chart with one cohort,
    grouped_bar_chart with two or more.
    A compatible `chart_hint` is honored. An incompatible one raises
    PlanValidationError(CHART_HINT_INCOMPATIBLE).
    """
    check_result_matches_plan(plan, aggregate_result)
    check_chart_hint(plan)
    return plan.chart_hint or default_chart_type(plan)
