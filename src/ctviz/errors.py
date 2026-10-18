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


class SourceRecordError(ValueError):
    """A raw study has a value the normalizer cannot interpret faithfully.

    Raised instead of guessing, so the caller can drop the record and report it in meta.
    """

    def __init__(self, nct_id: str, field_path: str, value: object, problem: str) -> None:
        super().__init__(f"{nct_id}: {field_path} = {value!r}: {problem}")
        self.nct_id = nct_id
        self.field_path = field_path
        self.value = value
