"""Size limits and dedup on request input, mirrored in Filters for LLM drafts (M3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ctviz.schemas.plan import (
    MAX_FILTER_TEXT_LENGTH,
    MAX_QUERY_LENGTH,
    Filters,
    LlmPlanDraft,
)
from ctviz.schemas.request import QueryRequest
from ctviz.schemas.trial import OverallStatus, Phase


def test_limits() -> None:
    assert MAX_QUERY_LENGTH == 2000
    assert MAX_FILTER_TEXT_LENGTH == 200


def test_long_query_is_rejected() -> None:
    QueryRequest(query="x" * MAX_QUERY_LENGTH)
    with pytest.raises(ValidationError):
        QueryRequest(query="x" * (MAX_QUERY_LENGTH + 1))


@pytest.mark.parametrize("name", ["drug", "condition", "sponsor", "country"])
def test_long_free_text_filter_is_rejected(name: str) -> None:
    QueryRequest(query="q", **{name: "x" * MAX_FILTER_TEXT_LENGTH})
    with pytest.raises(ValidationError):
        QueryRequest(query="q", **{name: "x" * (MAX_FILTER_TEXT_LENGTH + 1)})
    with pytest.raises(ValidationError):
        Filters(**{name: "x" * (MAX_FILTER_TEXT_LENGTH + 1)})


def test_lists_are_deduplicated_in_order() -> None:
    request = QueryRequest(query="q", phases=["PHASE2", "PHASE1", "PHASE2"])
    assert request.phases == [Phase.PHASE2, Phase.PHASE1]
    filters = Filters(statuses=[OverallStatus.RECRUITING, OverallStatus.RECRUITING])
    assert filters.statuses == [OverallStatus.RECRUITING]


def test_lists_longer_than_the_enum_are_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryRequest(query="q", phases=["PHASE1"] * (len(Phase) + 1))
    with pytest.raises(ValidationError):
        Filters(statuses=[OverallStatus.RECRUITING] * (len(OverallStatus) + 1))


def test_llm_schema_carries_no_length_keywords() -> None:
    # Strict structured output rejects unsupported keywords, so Filters checks its bounds
    # in validators instead of the JSON schema.
    schema = str(LlmPlanDraft.model_json_schema())
    assert "maxLength" not in schema
    assert "maxItems" not in schema


def test_bottom_n_is_a_request_field() -> None:
    request = QueryRequest(query="q", dimension="country", bottom_n=5)
    assert request.to_plan().bottom_n == 5
    with pytest.raises(ValidationError):
        QueryRequest(query="q", bottom_n=0)
    with pytest.raises(ValidationError):
        QueryRequest(query="q", bottom_n=1001)


def test_top_n_and_bottom_n_are_exclusive() -> None:
    with pytest.raises(ValidationError, match="bottom_n"):
        QueryRequest(query="q", top_n=3, bottom_n=3)
