"""The planner prompt, rendered from the registry so that prompt and code cannot drift.

Dimensions, relations and their descriptions come from `aggregate/registry.py`, chart
types and their compatibility from `spec/chart.py`, and filter values from the API
enums. Few-shot examples are `LlmPlanDraft` instances, so they always match the schema
the model must answer in (and a test checks that each one validates).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from openai.types.chat import ChatCompletionMessageParam

from ctviz.aggregate.registry import DIMENSIONS, RELATIONS
from ctviz.schemas.plan import (
    Clarification,
    Dimension,
    Filters,
    LlmPlanDraft,
    RankingDirection,
    Relation,
)
from ctviz.schemas.trial import OverallStatus, Phase, PrimaryPurpose, StudyType
from ctviz.spec.chart import CHART_DESCRIPTIONS, CHARTS_BY_KIND, NETWORK_CHARTS

ROLE = """\
You translate a question about clinical trials registered on ClinicalTrials.gov into a
query plan. You never answer the question and never produce numbers, counts, names of
results or any other data: code executes the plan against the live registry and counts
real trial records. The only metric is the number of trials.

The current year is {year}."""

PLAN_SHAPE = """\
A plan selects trials with filters (one filter set per cohort) and either counts trials
grouped by ONE dimension (`dimension`) or draws ONE relation as a network (`relation`).
Exactly one of the two is set. A relation works on a single cohort; comparisons of
several cohorts use a dimension."""

FILTERS = """\
All filters are optional and combined with AND.
- `drug`: an intervention name. With match_mode `intervention_name` (the default) it
  must appear in a trial's intervention names or its intervention MeSH terms; use
  match_mode `broad` only when the user
  asks for any mention of the drug.
- `condition`, `sponsor`, `country`: free text, matched by the registry's own search.
- `phases`: only when the user restricts the trials to phases ("phase 3 trials").
  Values: {phases}.
- `statuses`: only when the user restricts the recruitment status ("recruiting trials").
  Values: {statuses}.
- `start_year_from`, `start_year_to`: an inclusive range of start years. "since 2015" ->
  start_year_from 2015; "in 2020" -> both 2020; "between 2016 and 2019" -> 2016 and 2019;
  "in the last N years" -> start_year_from {year} - N + 1 and no end.
- `study_types`: only when the user names a study type ("interventional trials",
  "observational studies", "expanded access"). Values: {study_types}. "Trials" or
  "clinical trials" alone is not a study type.
- `exclude_withdrawn`: true only when the user asks to leave out withdrawn studies
  ("exclude withdrawn", "not withdrawn"); otherwise false.
- `primary_purposes`: only when the user names a primary purpose ("treatment studies",
  "prevention trials"). Values: {primary_purposes}."""

RULES = """\
1. Copy drug, condition, sponsor and country names verbatim from the question, with the
   user's spelling, brand or generic name and abbreviations. Never correct, expand,
   translate or replace a name: "Keytruda" stays "Keytruda", "NSCLC" stays "NSCLC".
   Leave out words that are not part of the name ("trials", "patients", "the").
2. Never add a filter the user did not state. "Trials" alone sets no status, phase,
   country, sponsor or year. Never add a bound the user did not state;
   'since 2015' means start_year_from=2015 and start_year_to=null (not the current year).
3. Use one cohort, unless the user compares groups ("A vs B", "compare A and B"): then
   one cohort per group, each with the shared filters plus its own value.
4. Choose the dimension or relation that answers the question. Leave `chart_hint` null
   unless the user names a chart type, and `top_n` null unless the user gives a number
   of values to show ("top 10", "the 5 with the fewest"); results are already sorted by
   count. Never choose a number yourself.
   Set `ranking_direction` to "most" only when the question asks for the most, top or
   leading values ("which countries have the most trials"), to "fewest" only when it
   asks for the fewest, least or lowest values ("which countries have the fewest
   trials"), otherwise null.
5. If the question cannot be answered by counting trials with these filters and
   groupings (for example efficacy, outcomes, survival, adverse events, costs, or
   anything about individual patients), set `unsupported_reason` to one sentence saying
   why, with no cohorts, dimension or relation.
6. If the question has two plausible readings that would give different charts (for
   example two groupings at once), set `clarification` with a short question and the
   readings as options, and leave dimension and relation null. Do not ask when one
   reading is clearly intended.
7. Filter values are plain text: no quotes, parentheses, brackets or AND/OR/NOT.
8. List in `unhandled_terms` every word or phrase of the question that narrows or
   reshapes the answer but that no filter, dimension or relation above expresses, copied
   from the question: for example "combination", "monotherapy", "first-line",
   "HER2-positive", or "mix" and "share" when the user asks for proportions. Still plan
   the rest of the question; code tells the user what was not applied. Do not list
   words already expressed by the plan ("trials", "by phase", a drug name). Leave it
   empty when every part of the question is mapped."""


def build_system_prompt(today: date) -> str:
    """The system prompt for `today`, listing everything the plan schema can express."""
    year = today.year
    sections = [
        ROLE.format(year=year),
        "## What a plan can express\n" + PLAN_SHAPE,
        "## Dimensions (`dimension`)\n" + render_dimensions(),
        "## Relations (`relation`, single cohort only)\n" + render_relations(),
        "## Chart types (`chart_hint`)\n" + render_charts(),
        "## Filters (per cohort)\n"
        + FILTERS.format(
            phases=render_values(Phase),
            statuses=render_values(OverallStatus),
            study_types=render_values(StudyType),
            primary_purposes=render_values(PrimaryPurpose),
            year=year,
        ),
        "## Rules\n" + RULES,
    ]
    return "\n\n".join(sections)


def render_dimensions() -> str:
    """One line per registered dimension, with its kind and overlap behavior."""
    lines = []
    for dimension, spec in DIMENSIONS.items():
        overlap = " A trial can count in several buckets." if spec.can_overlap else ""
        lines.append(f"- `{dimension.value}` ({spec.kind.value}): {spec.description}{overlap}")
    return "\n".join(lines)


def render_relations() -> str:
    """One line per registered relation."""
    return "\n".join(
        f"- `{relation.value}`: {spec.description}" for relation, spec in RELATIONS.items()
    )


def render_charts() -> str:
    """One line per chart type, with the dimension kinds (or relations) it can show."""
    lines = []
    for chart_type, description in CHART_DESCRIPTIONS.items():
        fits = [kind.value for kind, charts in CHARTS_BY_KIND.items() if chart_type in charts]
        if chart_type in NETWORK_CHARTS:
            fits.append("relations")
        lines.append(f"- `{chart_type.value}`: {description} Fits: {', '.join(fits)}.")
    return "\n".join(lines)


def render_values(values: Any) -> str:
    """Enum values as a comma-separated list of code spans."""
    return ", ".join(f"`{value.value}`" for value in values)


def example(question: str, *cohorts: Filters, **fields: Any) -> tuple[str, LlmPlanDraft]:
    """A few-shot pair; unset draft fields take their 'not stated' value."""
    values: dict[str, Any] = {
        "cohorts": list(cohorts),
        "dimension": None,
        "relation": None,
        "chart_hint": None,
        "top_n": None,
        "ranking_direction": None,
        "match_mode": "intervention_name",
        "unsupported_reason": None,
        "clarification": None,
        "unhandled_terms": [],
    }
    values.update(fields)
    return question, LlmPlanDraft(**values)


# One example per query class (trend, distribution, comparison, geographic, network),
# plus the two ways to decline. Names and wording differ from tests/eval/questions.yaml
# so the eval does not measure memorized examples.
FEW_SHOT_EXAMPLES: list[tuple[str, LlmPlanDraft]] = [
    example(
        "How many trials of atezolizumab started each month in 2023?",
        Filters(drug="atezolizumab", start_year_from=2023, start_year_to=2023),
        dimension=Dimension.START_MONTH,
    ),
    example(
        "What is the phase breakdown of first-line breast cancer combination trials "
        "sponsored by Novartis?",
        Filters(condition="breast cancer", sponsor="Novartis"),
        dimension=Dimension.PHASE,
        unhandled_terms=["first-line", "combination"],
    ),
    example(
        "How large are completed phase 3 type 2 diabetes trials?",
        Filters(
            condition="type 2 diabetes", phases=[Phase.PHASE3], statuses=[OverallStatus.COMPLETED]
        ),
        dimension=Dimension.ENROLLMENT_BUCKET,
    ),
    example(
        "Compare the recruitment status of durvalumab vs Tecentriq trials",
        Filters(drug="durvalumab"),
        Filters(drug="Tecentriq"),
        dimension=Dimension.OVERALL_STATUS,
    ),
    example(
        "Top 10 countries for recruiting Alzheimer's disease trials",
        Filters(condition="Alzheimer's disease", statuses=[OverallStatus.RECRUITING]),
        dimension=Dimension.COUNTRY,
        top_n=10,
        ranking_direction=RankingDirection.MOST,
    ),
    example(
        "Which drugs are most often combined with cisplatin in trials started since 2020?",
        Filters(drug="cisplatin", start_year_from=2020),
        relation=Relation.DRUG_DRUG_COOCCURRENCE,
        ranking_direction=RankingDirection.MOST,
    ),
    example(
        "What is the median overall survival in osimertinib trials?",
        unsupported_reason=(
            "Survival results are outcome data; the plan can only count trials by "
            "registry attributes."
        ),
    ),
    example(
        "Show melanoma trials by phase and by country",
        Filters(condition="melanoma"),
        clarification=Clarification(
            question="One chart groups by one attribute. Group by phase or by country?",
            options=["Melanoma trials by phase", "Melanoma trials by country"],
        ),
    ),
]


def build_messages(question: str, today: date) -> list[ChatCompletionMessageParam]:
    """System prompt, the few-shot pairs as user/assistant turns, then the question."""
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": build_system_prompt(today)}
    ]
    for example_question, draft in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": example_question})
        messages.append({"role": "assistant", "content": draft.model_dump_json()})
    messages.append({"role": "user", "content": question})
    return messages
