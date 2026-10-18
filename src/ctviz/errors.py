"""Exceptions shared across pipeline stages."""

from __future__ import annotations

from enum import StrEnum


class PlanErrorReason(StrEnum):
    """Machine-readable reason a QueryPlan was rejected."""

    NO_DIMENSION_OR_RELATION = "no_dimension_or_relation"
    BOTH_DIMENSION_AND_RELATION = "both_dimension_and_relation"
    COMPARISON_REQUIRES_DIMENSION = "comparison_requires_dimension"
    CHART_HINT_INCOMPATIBLE = "chart_hint_incompatible"
    INVALID_TOP_N = "invalid_top_n"
    TOP_N_REQUIRES_DIMENSION = "top_n_requires_dimension"
    INVALID_YEAR_RANGE = "invalid_year_range"


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


class InvalidFilterError(ValueError):
    """A filter value cannot be sent to the API without changing its meaning.

    Raised by the query builder, e.g. for Essie search syntax inside a free-text value.
    """

    def __init__(self, field: str, value: object, problem: str) -> None:
        super().__init__(f"filter {field} = {value!r}: {problem}")
        self.field = field
        self.value = value


class RetrievalError(Exception):
    """Base class for failures talking to ClinicalTrials.gov."""


class RateLimitError(RetrievalError):
    """The API kept answering HTTP 429 after every retry."""


class UpstreamError(RetrievalError):
    """The API failed (5xx, network error, or a malformed body) after every retry."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ApiRequestError(RetrievalError):
    """The API rejected the request (4xx other than 429); retrying would not help.

    `body` is the API's error text, which names the offending parameter.
    """

    def __init__(self, status_code: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status_code} from {url}: {body}")
        self.status_code = status_code
        self.body = body
