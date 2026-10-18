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
    DUPLICATE_COHORT = "duplicate_cohort"


class PlanValidationError(ValueError):
    """A QueryPlan cannot be answered faithfully as written.

    `reason` lets the API return a structured error instead of a fabricated chart.
    """

    def __init__(self, reason: PlanErrorReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PlannerErrorCode(StrEnum):
    """Machine-readable reason the planner could not produce a plan."""

    LLM_NOT_CONFIGURED = "llm_not_configured"
    LLM_UNAVAILABLE = "llm_unavailable"
    UNSUPPORTED_QUESTION = "unsupported_question"
    CLARIFICATION_NEEDED = "clarification_needed"
    PLANNING_FAILED = "planning_failed"


class PlannerError(Exception):
    """The question could not be turned into a valid QueryPlan.

    `suggestions` tell the caller how to proceed (e.g. the readings of an ambiguous
    question, or the structured fields to send instead).
    """

    def __init__(
        self, code: PlannerErrorCode, message: str, suggestions: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.suggestions = suggestions or []


class LlmInfrastructureError(PlannerError):
    """An LLM provider timed out, was unreachable, or answered 429/5xx after its retry.

    Only this failure moves the fallback chain to the next provider: the same question
    may well succeed elsewhere. Every other LLM failure (a rejected request, a refusal,
    an answer that breaks the schema) is a plain PlannerError and is not retried on
    another provider.
    """

    def __init__(self, provider: str, problem: str) -> None:
        super().__init__(
            PlannerErrorCode.LLM_UNAVAILABLE,
            f"The LLM planner request to {provider} failed: {problem}",
            ["Retry later.", "Or send structured fields (e.g. `drug` and `dimension`)."],
        )
        self.provider = provider
        self.problem = problem


# A raw value in an error message is cut to this many characters: a list-valued title or a
# whole object would otherwise make one warning line arbitrarily long (engineering M5).
MAX_VALUE_REPR_LENGTH = 120


def short_repr(value: object) -> str:
    """repr(value), cut to MAX_VALUE_REPR_LENGTH characters with a trailing ellipsis."""
    text = repr(value)
    if len(text) <= MAX_VALUE_REPR_LENGTH:
        return text
    return text[: MAX_VALUE_REPR_LENGTH - 1] + "…"


class SourceRecordError(ValueError):
    """A raw study has a value the normalizer cannot interpret faithfully.

    Raised instead of guessing, so the caller can drop the record and report it in meta.
    """

    def __init__(self, nct_id: str, field_path: str, value: object, problem: str) -> None:
        super().__init__(f"{nct_id}: {field_path} = {short_repr(value)}: {problem}")
        self.nct_id = nct_id
        self.field_path = field_path
        self.value = value


class UpstreamDataFormatError(Exception):
    """Too many retrieved records were unreadable to return a trustworthy chart.

    Raised when a cohort's skip ratio reaches REFUSE_SKIP_RATIO: at that point the API's
    data format has most likely changed, and a chart over the remaining records could
    mislead.
    """

    def __init__(self, cohort: str, retrieved: int, skipped: int, refuse_skip_ratio: float) -> None:
        super().__init__(
            "upstream data format may have changed; refusing to return a chart that could "
            f"mislead: {skipped} of {retrieved} retrieved trials ({skipped / retrieved:.1%}) "
            f"for cohort {cohort!r} could not be read (REFUSE_SKIP_RATIO="
            f"{refuse_skip_ratio:g})"
        )
        self.cohort = cohort
        self.retrieved = retrieved
        self.skipped = skipped
        self.refuse_skip_ratio = refuse_skip_ratio


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
