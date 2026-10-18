"""Evaluate the LLM planner against tests/eval/questions.yaml on one or more models.

Calls the real planner (prompt, merge, validation, retry) for each question and scores
the resulting plan field by field: outcome, cohort count, filters, grouping (dimension
or relation), granularity (temporal dimensions only) and top_n. Prints per-field
accuracy, latency and a diff for every miss, and optionally writes a Markdown report.

Run: uv run python scripts/eval_planner.py --models gpt-4.1-mini gpt-5-mini \\
         --output docs/planner_eval.md
The API key and base URL come from the usual settings (.env); they are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ctviz.aggregate.registry import DIMENSIONS, DimensionKind
from ctviz.config import Settings
from ctviz.errors import PlannerError, PlannerErrorCode
from ctviz.planner.llm import LlmClient, build_llm, parse_chain
from ctviz.planner.plan import plan
from ctviz.schemas.plan import Dimension, Filters, QueryPlan
from ctviz.schemas.request import QueryRequest

LIMITATIONS = """\
## Limitations

- 15 questions detect systematic failures (a query class the planner cannot map, a rule
  it ignores), not statistically robust accuracy: one miss moves a field by 7-8 points,
  and a perfect score does not mean the planner is always right.
- Results are for one prompt version on one day. Model outputs vary between runs at the
  same settings, providers update models behind the same name, and any change to the
  prompt, few-shot examples or registry descriptions needs a new run.
- Questions were written by the developer of the prompt, so they share its vocabulary;
  real users phrase questions differently and misspell names.
- Latency is wall-clock time from one machine, including the network and any retry."""

QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "tests" / "eval" / "questions.yaml"
FIELDS = ("outcome", "cohort_count", "filters", "grouping", "granularity", "top_n")
OUTCOME_BY_CODE = {
    PlannerErrorCode.UNSUPPORTED_QUESTION: "unsupported",
    PlannerErrorCode.CLARIFICATION_NEEDED: "clarification",
}

logger = logging.getLogger("eval_planner")


@dataclass
class CaseResult:
    """One question answered by one model, scored per field (None = not applicable)."""

    case_id: str
    query_class: str
    question: str
    latency_s: float
    outcome: str
    retries: int = 0
    detail: str | None = None
    scores: dict[str, bool | None] = field(default_factory=dict)
    misses: list[tuple[str, Any, Any]] = field(default_factory=list)


def load_cases(path: Path) -> list[dict[str, Any]]:
    """The eval cases, in file order."""
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def normalized_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Filters without empty values, strings casefolded and lists sorted, for comparison."""
    result: dict[str, Any] = {}
    for name, value in filters.items():
        if value is None or value == []:
            continue
        if isinstance(value, str):
            value = value.strip().casefold()
        elif isinstance(value, list):
            value = sorted(str(item) for item in value)
        result[name] = value
    return result


def cohort_filter_sets(query_plan: QueryPlan) -> list[dict[str, Any]]:
    """Each cohort's normalized filters, sorted so cohort order does not matter."""
    filters = [normalized_filters(c.filters.model_dump(mode="json")) for c in query_plan.cohorts]
    return sorted(filters, key=repr)


def expected_filter_sets(expect: dict[str, Any]) -> list[dict[str, Any]]:
    """The expected cohorts' filters, validated as Filters and normalized."""
    cohorts = [Filters.model_validate(c).model_dump(mode="json") for c in expect["cohorts"]]
    return sorted((normalized_filters(c) for c in cohorts), key=repr)


def granularity(dimension: str | None) -> str | None:
    """'year' or 'yearmonth' for temporal dimensions, else None."""
    if dimension is None:
        return None
    spec = DIMENSIONS[Dimension(dimension)]
    return spec.time_unit if spec.kind == DimensionKind.TEMPORAL else None


def score(result: CaseResult, expect: dict[str, Any], query_plan: QueryPlan | None) -> None:
    """Fill `result.scores` and `result.misses` for one case."""

    def check(name: str, expected: Any, actual: Any) -> None:
        matched = expected == actual
        result.scores[name] = matched
        if not matched:
            result.misses.append((name, expected, actual))

    check("outcome", expect["outcome"], result.outcome)
    for name in FIELDS[1:]:
        result.scores[name] = None
    if expect["outcome"] != "plan" or query_plan is None:
        return
    check("cohort_count", len(expect["cohorts"]), len(query_plan.cohorts))
    check("filters", expected_filter_sets(expect), cohort_filter_sets(query_plan))
    expected_grouping = expect.get("dimension") or expect.get("relation")
    check("grouping", expected_grouping, query_plan.dimension or query_plan.relation)
    expected_unit = granularity(expect.get("dimension"))
    if expected_unit is not None:
        check("granularity", expected_unit, granularity(query_plan.dimension))
    check("top_n", expect.get("top_n"), query_plan.top_n)


async def run_case(
    llm: LlmClient, case: dict[str, Any], today: date, default_top_n_for_ranking: int
) -> CaseResult:
    """Plan one question and score it."""
    started = time.perf_counter()
    query_plan: QueryPlan | None = None
    retries = 0
    detail: str | None = None
    try:
        interpretation = await plan(
            QueryRequest(query=case["question"]),
            llm,
            today=today,
            default_top_n_for_ranking=default_top_n_for_ranking,
        )
        query_plan, outcome = interpretation.plan, "plan"
        retries = len(interpretation.rejected_attempts)
    except PlannerError as error:
        outcome = OUTCOME_BY_CODE.get(error.code, f"error:{error.code.value}")
        detail = " ".join([str(error), *(f"[{s}]" for s in error.suggestions)])
        if error.code == PlannerErrorCode.LLM_UNAVAILABLE:
            logger.warning("%s: %s", case["id"], error)
    result = CaseResult(
        case_id=case["id"],
        query_class=case["class"],
        question=case["question"],
        latency_s=time.perf_counter() - started,
        outcome=outcome,
        retries=retries,
        detail=detail,
    )
    score(result, case["expect"], query_plan)
    return result


def field_accuracy(results: list[CaseResult]) -> dict[str, tuple[int, int]]:
    """Field -> (matches, applicable cases)."""
    accuracy: dict[str, tuple[int, int]] = {}
    for name in FIELDS:
        applicable = [r.scores[name] for r in results if r.scores.get(name) is not None]
        accuracy[name] = (sum(bool(s) for s in applicable), len(applicable))
    return accuracy


def render_report(by_model: dict[str, list[CaseResult]], today: date) -> str:
    """Markdown report: summary table, per-class results, and a diff for every miss."""
    lines = [
        "# Planner eval",
        "",
        f"Run on {today.isoformat()} with `scripts/eval_planner.py` over "
        f"`tests/eval/questions.yaml` ({len(next(iter(by_model.values())))} questions).",
        "Each question goes through the real planner (prompt, merge, validation, one retry).",
        "",
        "## Per-field accuracy",
        "",
        "| model | " + " | ".join(FIELDS) + " | all fields correct | mean latency | p50 | max "
        "| retries |",
        "|---|" + "---|" * (len(FIELDS) + 5),
    ]
    for model, results in by_model.items():
        accuracy = field_accuracy(results)
        cells = [f"{hits}/{total}" for hits, total in accuracy.values()]
        perfect = sum(not r.misses for r in results)
        latencies = [r.latency_s for r in results]
        lines.append(
            f"| `{model}` | "
            + " | ".join(cells)
            + f" | {perfect}/{len(results)} | {statistics.mean(latencies):.1f}s | "
            f"{statistics.median(latencies):.1f}s | {max(latencies):.1f}s | "
            f"{sum(r.retries for r in results)} |"
        )
    lines += ["", "## Per question", ""]
    models = list(by_model)
    lines.append("| id | class | " + " | ".join(f"`{m}`" for m in models) + " |")
    lines.append("|---|---|" + "---|" * len(models))
    for index, first in enumerate(by_model[models[0]]):
        cells = []
        for model in models:
            result = by_model[model][index]
            mark = "ok" if not result.misses else "miss: " + ", ".join(m[0] for m in result.misses)
            cells.append(f"{mark} ({result.latency_s:.1f}s)")
        lines.append(f"| {first.case_id} | {first.query_class} | " + " | ".join(cells) + " |")
    lines += ["", "## Misses", ""]
    for model, results in by_model.items():
        lines.append(f"### `{model}`")
        lines.append("")
        missed = [r for r in results if r.misses]
        if not missed:
            lines.append("No misses.")
        for result in missed:
            lines.append(f"- **{result.case_id}**: {result.question}")
            for name, expected, actual in result.misses:
                lines.append(f"  - `{name}`: expected `{expected}`, got `{actual}`")
            if result.detail:
                lines.append(f"  - planner said: {result.detail}")
        lines.append("")
    lines.append(LIMITATIONS)
    return "\n".join(lines)


def llm_for(settings: Settings, model: str) -> LlmClient:
    """An OpenAI-only chain for `model`, so each model is scored without fallback."""
    llm = build_llm(settings.model_copy(update={"llm_fallback_chain": f"openai:{model}"}))
    if llm is None:
        sys.exit("OPENAI_API_KEY is not set; the eval needs a real LLM.")
    return llm


async def main() -> None:
    """Run every case on every model and print (and optionally write) the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models", nargs="+", help="OpenAI models; defaults to the first in LLM_FALLBACK_CHAIN."
    )
    parser.add_argument("--ids", nargs="+", help="Only run the cases with these ids.")
    parser.add_argument("--output", type=Path, help="Write the Markdown report here.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    models = args.models or [parse_chain(settings.llm_fallback_chain)[0][1]]
    cases = [c for c in load_cases(QUESTIONS_PATH) if not args.ids or c["id"] in args.ids]
    today = date.today()
    by_model: dict[str, list[CaseResult]] = {}
    for model in models:
        llm = llm_for(settings, model)
        logger.info("Evaluating %s on %d questions", model, len(cases))
        by_model[model] = [
            await run_case(llm, case, today, settings.default_top_n_for_ranking) for case in cases
        ]
    report = render_report(by_model, today)
    sys.stdout.write(report + "\n")
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
        logger.info("Wrote %s", args.output)


if __name__ == "__main__":
    asyncio.run(main())
