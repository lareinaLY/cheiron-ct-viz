"""Tests for spec.select_chart."""

from __future__ import annotations

import pytest

from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.schemas.aggregate import Graph, GroupResult
from ctviz.schemas.plan import ChartType, Dimension, Relation
from ctviz.spec import select_chart
from tests.factories import make_plan


def group_result(dimension: Dimension) -> GroupResult:
    return GroupResult(dimension=dimension, buckets=[], buckets_overlap=False)


def graph(relation: Relation) -> Graph:
    return Graph(relation=relation, nodes=[], edges=[])


@pytest.mark.parametrize("dimension", [Dimension.START_YEAR, Dimension.START_MONTH])
def test_temporal_dimension_is_time_series(dimension: Dimension) -> None:
    plan = make_plan(dimension=dimension)
    assert select_chart(plan, group_result(dimension)) == ChartType.TIME_SERIES


@pytest.mark.parametrize(
    "dimension", [Dimension.PHASE, Dimension.COUNTRY, Dimension.SPONSOR_CLASS, Dimension.CONDITION]
)
def test_categorical_single_cohort_is_bar_chart(dimension: Dimension) -> None:
    plan = make_plan(dimension=dimension)
    assert select_chart(plan, group_result(dimension)) == ChartType.BAR_CHART


@pytest.mark.parametrize("cohort_count", [2, 3])
def test_categorical_multiple_cohorts_is_grouped_bar_chart(cohort_count: int) -> None:
    plan = make_plan(cohort_count, dimension=Dimension.PHASE)
    assert select_chart(plan, group_result(Dimension.PHASE)) == ChartType.GROUPED_BAR_CHART


@pytest.mark.parametrize("relation", list(Relation))
def test_relation_is_network_graph(relation: Relation) -> None:
    plan = make_plan(relation=relation)
    assert select_chart(plan, graph(relation)) == ChartType.NETWORK_GRAPH


def test_enrollment_bucket_is_histogram() -> None:
    plan = make_plan(dimension=Dimension.ENROLLMENT_BUCKET)
    assert select_chart(plan, group_result(Dimension.ENROLLMENT_BUCKET)) == ChartType.HISTOGRAM


def test_compatible_hint_is_honored() -> None:
    plan = make_plan(dimension=Dimension.START_YEAR, chart_hint=ChartType.BAR_CHART)
    assert select_chart(plan, group_result(Dimension.START_YEAR)) == ChartType.BAR_CHART


def test_hint_matching_default_is_honored() -> None:
    plan = make_plan(relation=Relation.SPONSOR_DRUG, chart_hint=ChartType.NETWORK_GRAPH)
    assert select_chart(plan, graph(Relation.SPONSOR_DRUG)) == ChartType.NETWORK_GRAPH


@pytest.mark.parametrize(
    ("plan_fields", "result"),
    [
        (
            {"relation": Relation.SPONSOR_DRUG, "chart_hint": ChartType.TIME_SERIES},
            graph(Relation.SPONSOR_DRUG),
        ),
        (
            {"dimension": Dimension.PHASE, "chart_hint": ChartType.NETWORK_GRAPH},
            group_result(Dimension.PHASE),
        ),
    ],
    ids=["time_series_for_relation", "network_for_dimension"],
)
def test_incompatible_hint_raises_with_reason(
    plan_fields: dict[str, object], result: GroupResult | Graph
) -> None:
    plan = make_plan(**plan_fields)
    with pytest.raises(PlanValidationError) as error:
        select_chart(plan, result)
    assert error.value.reason == PlanErrorReason.CHART_HINT_INCOMPATIBLE
    assert str(error.value)


def test_result_kind_must_match_plan() -> None:
    plan = make_plan(dimension=Dimension.PHASE)
    with pytest.raises(ValueError, match="phase"):
        select_chart(plan, graph(Relation.SPONSOR_DRUG))
