"""Exceptions shared across pipeline stages."""

from __future__ import annotations

from enum import StrEnum


class PlanErrorReason(StrEnum):
    """Machine-readable reason a QueryPlan was rejected."""

    NO_DIMENSION_OR_RELATION = "no_dimension_or_relation"
    BOTH_DIMENSION_AND_RELATION = "both_dimension_and_relation"
    COMPARISON_REQUIRES_DIMENSION = "comparison_requires_dimension"
    CHART_HINT_INCOMPATIBLE = "chart_hint_incompatible"


class PlanValidationError(ValueError):
    """A QueryPlan cannot be answered faithfully as written.

    `reason` lets the API return a structured error instead of a fabricated chart.
    """

    def __init__(self, reason: PlanErrorReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
