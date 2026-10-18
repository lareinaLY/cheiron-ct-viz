"""Tests for planner.validate_plan."""

from __future__ import annotations

import pytest

from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.planner import validate_plan
from ctviz.schemas.plan import ChartType, Cohort, Dimension, Filters, QueryPlan, Relation
from tests.factories import make_plan


def assert_rejected(plan: QueryPlan, reason: PlanErrorReason | None = None) -> None:
    with pytest.raises(PlanValidationError) as error:
        validate_plan(plan)
    if reason is not None:
        assert error.value.reason == reason
    assert str(error.value)


def test_neither_dimension_nor_relation_rejected() -> None:
    assert_rejected(make_plan(), PlanErrorReason.NO_DIMENSION_OR_RELATION)


def test_both_dimension_and_relation_rejected() -> None:
    plan = make_plan(dimension=Dimension.PHASE, relation=Relation.SPONSOR_DRUG)
    assert_rejected(plan, PlanErrorReason.BOTH_DIMENSION_AND_RELATION)


def test_relation_with_bar_chart_hint_rejected() -> None:
    plan = make_plan(relation=Relation.SPONSOR_DRUG, chart_hint=ChartType.BAR_CHART)
    assert_rejected(plan, PlanErrorReason.CHART_HINT_INCOMPATIBLE)


def test_comparison_without_dimension_or_relation_rejected() -> None:
    assert_rejected(make_plan(2))


def test_comparison_with_relation_but_no_dimension_rejected() -> None:
    plan = make_plan(2, relation=Relation.DRUG_DRUG_COOCCURRENCE)
    assert_rejected(plan, PlanErrorReason.COMPARISON_REQUIRES_DIMENSION)


@pytest.mark.parametrize(
    "plan",
    [
        make_plan(dimension=Dimension.PHASE),
        make_plan(dimension=Dimension.START_YEAR, time_granularity="year", top_n=10),
        make_plan(2, dimension=Dimension.COUNTRY, chart_hint=ChartType.GROUPED_BAR_CHART),
        make_plan(relation=Relation.SPONSOR_DRUG),
        make_plan(relation=Relation.CONDITION_DRUG, chart_hint=ChartType.NETWORK_GRAPH),
    ],
    ids=["phase", "start_year", "country_comparison", "sponsor_drug", "condition_drug_hint"],
)
def test_valid_plan_passes_unchanged(plan: QueryPlan) -> None:
    before = plan.model_dump()
    assert validate_plan(plan) == plan
    assert plan.model_dump() == before


@pytest.mark.parametrize("top_n", [0, -3])
def test_non_positive_top_n_rejected(top_n: int) -> None:
    plan = make_plan(dimension=Dimension.COUNTRY, top_n=top_n)
    assert_rejected(plan, PlanErrorReason.INVALID_TOP_N)


def test_top_n_with_relation_rejected() -> None:
    plan = make_plan(relation=Relation.SPONSOR_DRUG, top_n=10)
    assert_rejected(plan, PlanErrorReason.TOP_N_REQUIRES_DIMENSION)


def test_start_year_range_reversed_rejected() -> None:
    plan = QueryPlan(
        cohorts=[
            Cohort(label="ok", filters=Filters(drug="a")),
            Cohort(
                label="bad", filters=Filters(drug="b", start_year_from=2020, start_year_to=2015)
            ),
        ],
        dimension=Dimension.PHASE,
    )
    assert_rejected(plan, PlanErrorReason.INVALID_YEAR_RANGE)


def test_single_year_range_passes() -> None:
    plan = QueryPlan(
        cohorts=[Cohort(label="x", filters=Filters(start_year_from=2020, start_year_to=2020))],
        dimension=Dimension.START_YEAR,
    )
    assert validate_plan(plan) == plan
