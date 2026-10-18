"""Tests for the planner prompt: it is rendered from the registry, so it cannot drift."""

from __future__ import annotations

from datetime import date

import pytest

from ctviz.aggregate.registry import DIMENSIONS, RELATIONS
from ctviz.planner import validate_plan
from ctviz.planner.plan import merge_draft
from ctviz.planner.prompt import FEW_SHOT_EXAMPLES, build_messages, build_system_prompt
from ctviz.retrieval.query_builder import build_params
from ctviz.schemas.plan import ChartType, Dimension, LlmPlanDraft, RankingDirection, Relation
from ctviz.schemas.request import QueryRequest
from ctviz.schemas.trial import OverallStatus, Phase
from ctviz.spec.chart import CHART_DESCRIPTIONS

TODAY = date(2026, 10, 17)


@pytest.fixture(scope="module")
def prompt() -> str:
    return build_system_prompt(TODAY)


def test_every_dimension_has_a_description() -> None:
    assert set(DIMENSIONS) == set(Dimension)
    assert all(spec.description.strip() for spec in DIMENSIONS.values())


def test_every_relation_has_a_description() -> None:
    assert set(RELATIONS) == set(Relation)
    assert all(spec.description.strip() for spec in RELATIONS.values())


def test_every_chart_type_has_a_description() -> None:
    assert set(CHART_DESCRIPTIONS) == set(ChartType)
    assert all(text.strip() for text in CHART_DESCRIPTIONS.values())


def test_prompt_lists_every_dimension_with_its_description(prompt: str) -> None:
    for dimension, spec in DIMENSIONS.items():
        assert f"`{dimension.value}`" in prompt
        assert spec.description in prompt


def test_prompt_lists_every_relation_with_its_description(prompt: str) -> None:
    for relation, spec in RELATIONS.items():
        assert f"`{relation.value}`" in prompt
        assert spec.description in prompt


def test_prompt_lists_every_chart_type(prompt: str) -> None:
    for chart_type, description in CHART_DESCRIPTIONS.items():
        assert f"`{chart_type.value}`" in prompt
        assert description in prompt


def test_prompt_lists_every_status_and_phase_value(prompt: str) -> None:
    for value in [*OverallStatus, *Phase]:
        assert f"`{value.value}`" in prompt


def test_prompt_states_the_current_year(prompt: str) -> None:
    assert "2026" in prompt


def test_prompt_states_the_faithfulness_rules(prompt: str) -> None:
    lowered = prompt.lower()
    assert "verbatim" in lowered
    assert "unsupported_reason" in prompt
    assert "clarification" in prompt
    assert "never add a filter" in lowered


def test_prompt_states_the_ranking_rule(prompt: str) -> None:
    # Supersedes "fewest must not appear in the prompt": "fewest" questions are now a
    # ranking of their own (`ranking_direction` "fewest", trimmed to the bottom N), after
    # gpt-5-mini read "fewest" as "most" in the 2026-10-18 planner eval.
    assert "ranking_direction" in prompt
    assert "ranking_intent" not in prompt
    for word in ("most", "top", "leading", "fewest", "least", "lowest"):
        assert word in prompt


def test_a_few_shot_example_flags_a_ranking_without_a_number() -> None:
    drafts = [draft for _, draft in FEW_SHOT_EXAMPLES]
    assert any(d.ranking_direction == RankingDirection.MOST and d.top_n is not None for d in drafts)
    assert any(d.ranking_direction is None and d.dimension is not None for d in drafts)


def test_few_shot_examples_cover_the_query_classes() -> None:
    drafts = [draft for _, draft in FEW_SHOT_EXAMPLES]
    assert 6 <= len(drafts) <= 8
    dimensions = {draft.dimension for draft in drafts}
    assert any(DIMENSIONS[d].kind == "temporal" for d in dimensions if d)
    assert Dimension.COUNTRY in dimensions
    assert any(len(draft.cohorts) > 1 for draft in drafts)
    assert any(draft.relation is not None for draft in drafts)
    assert any(draft.unsupported_reason for draft in drafts)
    assert any(draft.clarification for draft in drafts)


@pytest.mark.parametrize(
    ("question", "draft"),
    [
        example
        for example in FEW_SHOT_EXAMPLES
        if example[1].unsupported_reason is None and example[1].clarification is None
    ],
    ids=lambda value: value[:40] if isinstance(value, str) else "",
)
def test_answerable_few_shot_examples_produce_valid_plans(
    question: str, draft: LlmPlanDraft
) -> None:
    plan, _ = merge_draft(draft, QueryRequest(query=question))
    validate_plan(plan)
    for cohort in plan.cohorts:
        build_params(cohort.filters, plan.match_mode)


def test_messages_end_with_the_question_after_the_examples() -> None:
    messages = build_messages("How many trials per year?", TODAY)
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "How many trials per year?"}
    assert len(messages) == 2 + 2 * len(FEW_SHOT_EXAMPLES)


def test_prompt_asks_for_unhandled_terms(prompt: str) -> None:
    assert "unhandled_terms" in prompt
    assert "combination" in prompt


def test_a_few_shot_example_lists_an_unhandled_term() -> None:
    assert any(draft.unhandled_terms for _, draft in FEW_SHOT_EXAMPLES)


def test_prompt_forbids_unstated_year_bounds(prompt: str) -> None:
    assert "Never add a bound the user did not state" in prompt
    assert "'since 2015' means start_year_from=2015 and start_year_to=null" in prompt
