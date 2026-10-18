"""Tests for planner.plan with a fake LLM: merge, overrides, retry, fallback and errors."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from ctviz.config import Settings
from ctviz.errors import PlannerError, PlannerErrorCode
from ctviz.planner.llm import LlmAnswer, build_llm
from ctviz.planner.plan import plan
from ctviz.schemas.plan import (
    Clarification,
    Dimension,
    Filters,
    LlmPlanDraft,
    Relation,
)
from ctviz.schemas.request import QueryRequest
from ctviz.schemas.response import ResponseMode
from ctviz.schemas.trial import OverallStatus, Phase

TODAY = date(2026, 10, 17)


class FakeLlm:
    """Returns queued drafts in order and records the messages of every call."""

    model = "fake-model"

    def __init__(self, *drafts: LlmPlanDraft) -> None:
        self.drafts = list(drafts)
        self.calls: list[list[Any]] = []

    async def structured_plan(self, messages: Sequence[Any], schema: type[Any]) -> Any:
        assert schema is LlmPlanDraft
        self.calls.append(list(messages))
        return LlmAnswer(
            value=self.drafts.pop(0),
            provider="fake",
            model=self.model,
            response_mode=ResponseMode.JSON_SCHEMA_STRICT,
        )


def draft(*cohorts: Filters, **fields: Any) -> LlmPlanDraft:
    """A draft with every field set to its 'not stated' value unless given."""
    values: dict[str, Any] = {
        "cohorts": list(cohorts),
        "dimension": None,
        "relation": None,
        "chart_hint": None,
        "top_n": None,
        "ranking_intent": False,
        "match_mode": "intervention_name",
        "unsupported_reason": None,
        "clarification": None,
    }
    values.update(fields)
    return LlmPlanDraft(**values)


PEMBRO_BY_PHASE = draft(Filters(drug="pembrolizumab"), dimension=Dimension.PHASE)


async def run_plan(request: QueryRequest, llm: FakeLlm | None) -> Any:
    return await plan(request, llm, today=TODAY)


class TestLlmPlanning:
    async def test_draft_becomes_the_plan(self) -> None:
        llm = FakeLlm(PEMBRO_BY_PHASE)
        result = await run_plan(QueryRequest(query="pembrolizumab by phase"), llm)
        assert result.planned_by == "llm"
        assert result.model == "fake-model"
        assert result.draft == PEMBRO_BY_PHASE
        assert result.question == "pembrolizumab by phase"
        assert result.plan.dimension == Dimension.PHASE
        assert result.plan.cohorts[0].filters == Filters(drug="pembrolizumab")
        assert result.overrides == []
        assert result.rejected_attempts == []
        assert result.planning_ms is not None
        assert len(llm.calls) == 1
        assert llm.calls[0][-1] == {"role": "user", "content": "pembrolizumab by phase"}

    async def test_cohort_labels_are_derived_from_filters(self) -> None:
        llm = FakeLlm(
            draft(
                Filters(drug="pembrolizumab"),
                Filters(drug="nivolumab", statuses=[OverallStatus.RECRUITING]),
                dimension=Dimension.PHASE,
            )
        )
        result = await run_plan(QueryRequest(query="q"), llm)
        assert [c.label for c in result.plan.cohorts] == ["pembrolizumab", "nivolumab, recruiting"]


class TestExplicitFieldsOverride:
    async def test_explicit_filter_replaces_llm_filter(self) -> None:
        request = QueryRequest(query="pembrolizumab by phase", drug="nivolumab")
        result = await run_plan(request, FakeLlm(PEMBRO_BY_PHASE))
        cohort = result.plan.cohorts[0]
        assert cohort.filters.drug == "nivolumab"
        assert cohort.label == "nivolumab"
        assert [(o.path, o.llm_value, o.explicit_value) for o in result.overrides] == [
            ("cohorts[0].filters.drug", "pembrolizumab", "nivolumab")
        ]

    async def test_explicit_filter_applies_to_every_cohort(self) -> None:
        llm = FakeLlm(draft(Filters(drug="a"), Filters(drug="b"), dimension=Dimension.PHASE))
        result = await run_plan(QueryRequest(query="q", country="Germany"), llm)
        assert [c.filters.country for c in result.plan.cohorts] == ["Germany", "Germany"]
        assert [c.filters.drug for c in result.plan.cohorts] == ["a", "b"]

    async def test_llm_filters_kept_when_not_explicit(self) -> None:
        llm = FakeLlm(draft(Filters(drug="x", condition="melanoma"), dimension=Dimension.PHASE))
        result = await run_plan(QueryRequest(query="q", drug="y"), llm)
        assert result.plan.cohorts[0].filters == Filters(drug="y", condition="melanoma")

    async def test_explicit_empty_list_overrides_llm_list(self) -> None:
        llm = FakeLlm(draft(Filters(drug="x", phases=[Phase.PHASE3]), dimension=Dimension.PHASE))
        result = await run_plan(QueryRequest(query="q", phases=[]), llm)
        assert result.plan.cohorts[0].filters.phases == []
        assert result.overrides[0].path == "cohorts[0].filters.phases"

    async def test_explicit_dimension_replaces_llm_relation(self) -> None:
        llm = FakeLlm(draft(Filters(drug="x"), relation=Relation.SPONSOR_DRUG))
        result = await run_plan(QueryRequest(query="q", dimension=Dimension.PHASE), llm)
        assert result.plan.dimension == Dimension.PHASE
        assert result.plan.relation is None
        assert {o.path for o in result.overrides} == {"dimension", "relation"}

    async def test_explicit_relation_replaces_llm_dimension(self) -> None:
        llm = FakeLlm(PEMBRO_BY_PHASE)
        request = QueryRequest(query="q", relation=Relation.SPONSOR_DRUG)
        result = await run_plan(request, llm)
        assert result.plan.relation == Relation.SPONSOR_DRUG
        assert result.plan.dimension is None

    async def test_explicit_plan_options_override(self) -> None:
        llm = FakeLlm(
            draft(Filters(drug="x"), dimension=Dimension.COUNTRY, top_n=5, match_mode="broad")
        )
        request = QueryRequest(query="q", top_n=10, match_mode="intervention_name")
        result = await run_plan(request, llm)
        assert result.plan.top_n == 10
        assert result.plan.match_mode == "intervention_name"

    async def test_default_match_mode_is_not_an_explicit_field(self) -> None:
        llm = FakeLlm(draft(Filters(drug="x"), dimension=Dimension.PHASE, match_mode="broad"))
        result = await run_plan(QueryRequest(query="q"), llm)
        assert result.plan.match_mode == "broad"
        assert result.overrides == []


class TestRankingIntent:
    COUNTRIES = draft(Filters(condition="NSCLC"), dimension=Dimension.COUNTRY, ranking_intent=True)

    async def test_ranking_without_a_number_gets_the_default_top_n(self) -> None:
        result = await run_plan(
            QueryRequest(query="Which countries have the most?"), FakeLlm(self.COUNTRIES)
        )
        assert result.plan.top_n == 15
        assert result.ranking_top_n == 15

    async def test_default_comes_from_configuration(self) -> None:
        request = QueryRequest(query="Which countries have the most?")
        result = await plan(
            request, FakeLlm(self.COUNTRIES), today=TODAY, default_top_n_for_ranking=8
        )
        assert result.plan.top_n == 8
        assert result.ranking_top_n == 8

    async def test_number_in_the_question_wins(self) -> None:
        llm = FakeLlm(self.COUNTRIES.model_copy(update={"top_n": 5}))
        result = await run_plan(QueryRequest(query="Top 5 countries"), llm)
        assert result.plan.top_n == 5
        assert result.ranking_top_n is None

    async def test_explicit_top_n_wins(self) -> None:
        request = QueryRequest(query="Which countries have the most?", top_n=7)
        result = await run_plan(request, FakeLlm(self.COUNTRIES))
        assert result.plan.top_n == 7
        assert result.ranking_top_n is None

    async def test_no_ranking_no_top_n(self) -> None:
        llm = FakeLlm(self.COUNTRIES.model_copy(update={"ranking_intent": False}))
        result = await run_plan(QueryRequest(query="Where are NSCLC trials?"), llm)
        assert result.plan.top_n is None
        assert result.ranking_top_n is None

    async def test_relation_is_never_trimmed(self) -> None:
        network = draft(
            Filters(drug="cisplatin"),
            relation=Relation.DRUG_DRUG_COOCCURRENCE,
            ranking_intent=True,
        )
        result = await run_plan(QueryRequest(query="most often combined with"), FakeLlm(network))
        assert result.plan.top_n is None
        assert result.ranking_top_n is None
        assert result.rejected_attempts == []


class TestRetryOnValidationFailure:
    async def test_invalid_draft_is_retried_with_the_error(self) -> None:
        llm = FakeLlm(draft(Filters(drug="x")), PEMBRO_BY_PHASE)
        result = await run_plan(QueryRequest(query="q"), llm)
        assert result.plan.dimension == Dimension.PHASE
        assert len(llm.calls) == 2
        feedback = llm.calls[1][-1]
        assert feedback["role"] == "user"
        assert "dimension" in feedback["content"]
        assert llm.calls[1][-2]["role"] == "assistant"
        assert len(result.rejected_attempts) == 1
        assert "dimension" in result.rejected_attempts[0]

    async def test_invalid_filter_value_is_retried(self) -> None:
        bad = draft(Filters(condition="lung cancer OR melanoma"), dimension=Dimension.PHASE)
        llm = FakeLlm(bad, draft(Filters(condition="lung cancer"), dimension=Dimension.PHASE))
        result = await run_plan(QueryRequest(query="q"), llm)
        assert result.plan.cohorts[0].filters.condition == "lung cancer"
        assert "OR" in result.rejected_attempts[0]

    async def test_draft_without_cohorts_is_retried(self) -> None:
        llm = FakeLlm(draft(dimension=Dimension.PHASE), PEMBRO_BY_PHASE)
        result = await run_plan(QueryRequest(query="q"), llm)
        assert len(llm.calls) == 2
        assert result.plan.cohorts[0].filters.drug == "pembrolizumab"

    async def test_second_failure_is_a_structured_error(self) -> None:
        llm = FakeLlm(draft(Filters(drug="x")), draft(Filters(drug="x")))
        with pytest.raises(PlannerError) as error:
            await run_plan(QueryRequest(query="q"), llm)
        assert error.value.code == PlannerErrorCode.PLANNING_FAILED
        assert "dimension" in str(error.value)
        assert len(llm.calls) == 2

    async def test_override_that_duplicates_cohorts_fails(self) -> None:
        comparison = draft(Filters(drug="a"), Filters(drug="b"), dimension=Dimension.PHASE)
        llm = FakeLlm(comparison, comparison)
        with pytest.raises(PlannerError) as error:
            await run_plan(QueryRequest(query="a vs b", drug="a"), llm)
        assert error.value.code == PlannerErrorCode.PLANNING_FAILED
        assert "duplicate" in str(error.value).lower()


class TestUnanswerableQuestions:
    async def test_unsupported_reason_is_a_structured_error(self) -> None:
        llm = FakeLlm(draft(unsupported_reason="Outcome results are not available."))
        with pytest.raises(PlannerError) as error:
            await run_plan(QueryRequest(query="average response rate?"), llm)
        assert error.value.code == PlannerErrorCode.UNSUPPORTED_QUESTION
        assert "Outcome results are not available." in str(error.value)
        assert len(llm.calls) == 1

    async def test_clarification_returns_the_options(self) -> None:
        options = ["Group by phase", "Group by country"]
        clarification = Clarification(question="Which grouping?", options=options)
        llm = FakeLlm(draft(Filters(condition="melanoma"), clarification=clarification))
        with pytest.raises(PlannerError) as error:
            await run_plan(QueryRequest(query="melanoma by phase and country"), llm)
        assert error.value.code == PlannerErrorCode.CLARIFICATION_NEEDED
        assert "Which grouping?" in str(error.value)
        assert error.value.suggestions == options


class TestRulesFallback:
    async def test_structured_fields_are_planned_by_rules(self) -> None:
        request = QueryRequest(query="q", drug="pembrolizumab", dimension=Dimension.PHASE)
        result = await run_plan(request, None)
        assert result.planned_by == "rules"
        assert result.model is None
        assert result.draft is None
        assert result.plan == request.to_plan()

    async def test_question_needing_interpretation_is_an_error(self) -> None:
        with pytest.raises(PlannerError) as error:
            await run_plan(QueryRequest(query="pembrolizumab trials per year"), None)
        assert error.value.code == PlannerErrorCode.LLM_NOT_CONFIGURED
        assert "OPENAI_API_KEY" in str(error.value)
        assert error.value.suggestions

    def test_no_llm_without_api_key(self) -> None:
        settings = Settings(_env_file=None, openai_api_key=None)  # type: ignore[call-arg]
        assert build_llm(settings) is None

    def test_llm_built_with_api_key(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, openai_api_key="sk-test", llm_model="gpt-4.1-mini"
        )
        llm = build_llm(settings)
        assert llm is not None
        assert llm.model == "gpt-4.1-mini"

    def test_non_ascii_api_key_fails_fast_without_echoing_it(self) -> None:
        settings = Settings(_env_file=None, openai_api_key="你的key")  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="OPENAI_API_KEY") as error:
            build_llm(settings)
        assert "你的key" not in str(error.value)
