"""Tests for planner.validate_plan."""

from __future__ import annotations

import pytest

from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.planner import validate_plan
from ctviz.schemas.plan import ChartType, Dimension, QueryPlan, Relation
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
