"""Structural validation of a QueryPlan before any retrieval happens."""

from __future__ import annotations

from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.schemas.plan import MAX_START_YEAR, MAX_TOP_N, MIN_START_YEAR, QueryPlan
from ctviz.spec.chart import check_chart_hint


def validate_plan(plan: QueryPlan) -> QueryPlan:
    """Return `plan` unchanged if it can be answered, else raise PlanValidationError.

    Rejects: neither dimension nor relation; both set; a relation combined with two or
    more cohorts (comparisons need a dimension); a chart_hint incompatible with the
    dimension or relation (e.g. bar_chart for a relation); a top_n or bottom_n outside
    1..MAX_TOP_N, both set, or either combined with a relation (they trim buckets, and
    networks have none); a start
    year outside MIN_START_YEAR..MAX_START_YEAR, or a cohort whose start_year_from is
    after its start_year_to; two cohorts with the same label or the
    same filters (results are keyed by label, and identical cohorts compare nothing).
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
    check_distinct_cohorts(plan)
    return plan


def check_top_n(plan: QueryPlan) -> None:
    """Raise PlanValidationError if top_n or bottom_n cannot be applied as written."""
    if plan.top_n is not None and plan.bottom_n is not None:
        raise PlanValidationError(
            PlanErrorReason.INVALID_TOP_N,
            "top_n and bottom_n cannot both be set; a ranking keeps one end.",
        )
    for name, value in (("top_n", plan.top_n), ("bottom_n", plan.bottom_n)):
        if value is None:
            continue
        if not 1 <= value <= MAX_TOP_N:
            raise PlanValidationError(
                PlanErrorReason.INVALID_TOP_N,
                f"{name} must be between 1 and {MAX_TOP_N}, got {value}.",
            )
        if plan.relation is not None:
            raise PlanValidationError(
                PlanErrorReason.TOP_N_REQUIRES_DIMENSION,
                f"{name} limits the buckets of a dimension; relation {plan.relation} has none.",
            )


def check_year_ranges(plan: QueryPlan) -> None:
    """Raise PlanValidationError for an out-of-range start year or an empty range."""
    for cohort in plan.cohorts:
        start, end = cohort.filters.start_year_from, cohort.filters.start_year_to
        for name, year in (("start_year_from", start), ("start_year_to", end)):
            if year is not None and not MIN_START_YEAR <= year <= MAX_START_YEAR:
                raise PlanValidationError(
                    PlanErrorReason.INVALID_YEAR_RANGE,
                    f"Cohort '{cohort.label}': {name} must be between {MIN_START_YEAR} and "
                    f"{MAX_START_YEAR}, got {year}.",
                )
        if start is not None and end is not None and start > end:
            raise PlanValidationError(
                PlanErrorReason.INVALID_YEAR_RANGE,
                f"Cohort '{cohort.label}': start_year_from {start} is after start_year_to {end}.",
            )


def check_distinct_cohorts(plan: QueryPlan) -> None:
    """Raise PlanValidationError if two cohorts share a label or their filters."""
    labels = [cohort.label for cohort in plan.cohorts]
    for label in labels:
        if labels.count(label) > 1:
            raise PlanValidationError(
                PlanErrorReason.DUPLICATE_COHORT,
                f"Duplicate cohort label '{label}'; every cohort needs its own label.",
            )
    filters = [cohort.filters for cohort in plan.cohorts]
    for index, cohort in enumerate(plan.cohorts):
        if cohort.filters in filters[:index]:
            raise PlanValidationError(
                PlanErrorReason.DUPLICATE_COHORT,
                f"Cohort '{cohort.label}' has the same filters as an earlier cohort "
                "(duplicate cohorts); a comparison needs cohorts that differ.",
            )
