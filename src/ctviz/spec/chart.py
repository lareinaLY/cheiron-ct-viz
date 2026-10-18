"""Deterministic chart-type selection."""

from __future__ import annotations

from ctviz.schemas.aggregate import Graph, GroupResult
from ctviz.schemas.plan import ChartType, QueryPlan


def select_chart(plan: QueryPlan, aggregate_result: GroupResult | Graph) -> ChartType:
    """Pick the chart type for a validated plan.

    Defaults: relation -> network_graph; start_year/start_month -> time_series;
    enrollment_bucket -> histogram; other dimensions -> bar_chart with one cohort,
    grouped_bar_chart with two or more.
    A compatible `chart_hint` is honored. An incompatible one raises
    PlanValidationError(CHART_HINT_INCOMPATIBLE).
    """
    raise NotImplementedError("select_chart is not implemented yet")
