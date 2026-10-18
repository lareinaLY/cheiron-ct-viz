"""Structural validation of a QueryPlan before any retrieval happens."""

from __future__ import annotations

from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.schemas.plan import QueryPlan
from ctviz.spec.chart import check_chart_hint


def validate_plan(plan: QueryPlan) -> QueryPlan:
    """Return `plan` unchanged if it can be answered, else raise PlanValidationError.

    Rejects: neither dimension nor relation; both set; a relation combined with two or
    more cohorts (comparisons need a dimension); a chart_hint incompatible with the
    dimension or relation (e.g. bar_chart for a relation).
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
    return plan
