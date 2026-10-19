"""Recruiting is a study-level status; a country chart cannot say each site recruits."""

from __future__ import annotations

from ctviz.pipeline import recruiting_country_warnings
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import NoticeCode, NoticeSeverity
from ctviz.schemas.trial import OverallStatus

MESSAGE = (
    "Recruiting is the study's overall status; a listed country may have sites that are "
    "not yet or no longer recruiting."
)
RECRUITING = Filters(condition="NSCLC", statuses=[OverallStatus.RECRUITING])


def plan(*filters: Filters, dimension: Dimension) -> QueryPlan:
    cohorts = [Cohort(label=f"c{i}", filters=f) for i, f in enumerate(filters)]
    return QueryPlan(cohorts=cohorts, dimension=dimension)


def test_recruiting_cohort_by_country_is_warned() -> None:
    notices = recruiting_country_warnings(
        plan(Filters(condition="NSCLC"), RECRUITING, dimension=Dimension.COUNTRY)
    )
    assert [(n.code, n.severity, n.message) for n in notices] == [
        (NoticeCode.RECRUITING_IS_STUDY_LEVEL, NoticeSeverity.WARNING, MESSAGE)
    ]


def test_no_warning_without_recruiting_or_off_the_country_axis() -> None:
    completed = Filters(condition="NSCLC", statuses=[OverallStatus.COMPLETED])
    assert recruiting_country_warnings(plan(completed, dimension=Dimension.COUNTRY)) == []
    assert recruiting_country_warnings(plan(RECRUITING, dimension=Dimension.PHASE)) == []
