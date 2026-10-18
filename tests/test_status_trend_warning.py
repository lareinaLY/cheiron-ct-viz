"""A status filter on a time axis: today's status, so older years are under-represented."""

from __future__ import annotations

import pytest

from ctviz.pipeline import status_trend_warnings
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan, Relation
from ctviz.schemas.trial import OverallStatus

WARNING = (
    "Status reflects today; older trials have left this status, so earlier years are "
    "under-represented. This is not a trend in activity."
)
RECRUITING = Filters(condition="NSCLC", statuses=[OverallStatus.RECRUITING])


def plan(*filters: Filters, **fields: object) -> QueryPlan:
    cohorts = [Cohort(label=f"c{i}", filters=f) for i, f in enumerate(filters)]
    return QueryPlan(cohorts=cohorts, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("dimension", [Dimension.START_YEAR, Dimension.START_MONTH])
def test_status_filter_on_a_time_axis_is_warned(dimension: Dimension) -> None:
    assert status_trend_warnings(plan(RECRUITING, dimension=dimension)) == [WARNING]


def test_one_cohort_with_a_status_filter_is_enough() -> None:
    query_plan = plan(Filters(condition="NSCLC"), RECRUITING, dimension=Dimension.START_YEAR)
    assert status_trend_warnings(query_plan) == [WARNING]


def test_no_status_filter_no_warning() -> None:
    query_plan = plan(Filters(condition="NSCLC"), dimension=Dimension.START_YEAR)
    assert status_trend_warnings(query_plan) == []


def test_non_temporal_dimension_no_warning() -> None:
    assert status_trend_warnings(plan(RECRUITING, dimension=Dimension.PHASE)) == []


def test_relation_no_warning() -> None:
    assert status_trend_warnings(plan(RECRUITING, relation=Relation.SPONSOR_DRUG)) == []
