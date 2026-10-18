"""Structural validation of a QueryPlan before any retrieval happens."""

from __future__ import annotations

from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.schemas.plan import QueryPlan
from ctviz.spec.chart import check_chart_hint


def validate_plan(plan: QueryPlan) -> QueryPlan:
    """Return `plan` unchanged if it can be answered, else raise PlanValidationError.

    Rejects: neither dimension nor relation; both set; a relation combined with two or
    more cohorts (comparisons need a dimension); a chart_hint incompatible with the
    dimension or relation (e.g. bar_chart for a relation); a top_n below 1, or combined
    with a relation (top_n trims buckets, and networks have none); a cohort whose
    start_year_from is after its start_year_to.
    """
    if plan.dimension is None and plan.relation is None:
        raise PlanValidationError(
            PlanErrorReason.NO_DIMENSION_OR_RELATION,
            "The plan must set a dimension (to group trials) or a relation (to build a network).",
        )
    if plan.dimension is not None and plan.relation is not None:
        raise PlanValidationError(
            PlanErrorReason.BOTH_DIMENSION_AND_RELATION,
            f"The plan sets both dimension {plan.dimension} and relation {plan.relation}; "
            "choose one.",
        )
    if plan.relation is not None and len(plan.cohorts) > 1:
        raise PlanValidationError(
            PlanErrorReason.COMPARISON_REQUIRES_DIMENSION,
            f"Comparing {len(plan.cohorts)} cohorts needs a dimension; "
            f"relation {plan.relation} supports a single cohort.",
        )
    check_chart_hint(plan)
    check_top_n(plan)
    check_year_ranges(plan)
    return plan


def check_top_n(plan: QueryPlan) -> None:
    """Raise PlanValidationError if top_n cannot be applied as written."""
    if plan.top_n is None:
        return
    if plan.top_n < 1:
        raise PlanValidationError(
            PlanErrorReason.INVALID_TOP_N, f"top_n must be at least 1, got {plan.top_n}."
        )
    if plan.relation is not None:
        raise PlanValidationError(
            PlanErrorReason.TOP_N_REQUIRES_DIMENSION,
            f"top_n limits the buckets of a dimension; relation {plan.relation} has none.",
        )


def check_year_ranges(plan: QueryPlan) -> None:
    """Raise PlanValidationError for a cohort whose start-year range is empty."""
    for cohort in plan.cohorts:
        start, end = cohort.filters.start_year_from, cohort.filters.start_year_to
        if start is not None and end is not None and start > end:
            raise PlanValidationError(
                PlanErrorReason.INVALID_YEAR_RANGE,
                f"Cohort '{cohort.label}': start_year_from {start} is after start_year_to {end}.",
            )
