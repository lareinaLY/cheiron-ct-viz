"""Structural validation of a QueryPlan before any retrieval happens."""

from __future__ import annotations

from ctviz.schemas.plan import QueryPlan


def validate_plan(plan: QueryPlan) -> QueryPlan:
    """Return `plan` unchanged if it can be answered, else raise PlanValidationError.

    Rejects: neither dimension nor relation; both set; a relation combined with two or
    more cohorts (comparisons need a dimension); a chart_hint incompatible with the
    dimension or relation (e.g. bar_chart for a relation).
    """
    raise NotImplementedError("validate_plan is not implemented yet")
