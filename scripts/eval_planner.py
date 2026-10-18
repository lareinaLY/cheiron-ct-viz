"""Evaluate the LLM planner against tests/eval/questions.yaml on one or more models.

Calls the real planner (prompt, merge, validation, retry) for each question and scores
the resulting plan field by field: outcome, cohort count, filters, grouping (dimension
or relation), granularity (temporal dimensions only), top_n and unhandled terms. Each
case can be run several times (`--repeats`) to measure stability, and each seed question
can be paraphrased by the configured LLM (`--paraphrases`); paraphrases are generated
once, cached in tests/eval/paraphrases.yaml and scored against the seed's expectations.
Prints per-field accuracy, stability, latency and a diff for every miss, and optionally
writes a Markdown report.

Run: uv run python scripts/eval_planner.py --models gpt-4.1-mini gpt-5-mini \\
         --repeats 3 --paraphrases 2 --output docs/planner_eval.md
The API key and base URL come from the usual settings (.env); they are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from ctviz.aggregate.registry import DIMENSIONS, DimensionKind
from ctviz.config import Settings
from ctviz.errors import PlannerError, PlannerErrorCode
from ctviz.planner.llm import LlmClient, build_llm, parse_chain
from ctviz.planner.plan import plan
from ctviz.schemas.plan import Dimension, Filters, QueryPlan
from ctviz.schemas.request import QueryRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = REPO_ROOT / "tests" / "eval" / "questions.yaml"
PARAPHRASES_PATH = REPO_ROOT / "tests" / "eval" / "paraphrases.yaml"
PROMPT_PATH = REPO_ROOT / "src" / "ctviz" / "planner" / "prompt.py"
FIELDS = (
    "outcome",
    "cohort_count",
    "filters",
    "grouping",
    "granularity",
    "top_n",
    "bottom_n",
    "ranking_direction",
    "unhandled_terms",
)
OUTCOME_BY_CODE = {
    PlannerErrorCode.UNSUPPORTED_QUESTION: "unsupported",
    PlannerErrorCode.CLARIFICATION_NEEDED: "clarification",
}
PARAPHRASE_INSTRUCTIONS = """\
You rewrite questions about clinical trials registered on ClinicalTrials.gov the way
different real users might ask them. Give exactly {count} rewrites of the user's question.
Each rewrite must ask for exactly the same thing: keep every drug, condition, sponsor
and country name exactly as written (same spelling, misspellings and abbreviations
included), keep every number, year and status, and add or remove no constraint. Vary
the wording and sentence structure only. Write in the language of the question."""

logger = logging.getLogger("eval_planner")


class Paraphrases(BaseModel):
    """The LLM's rewrites of one seed question."""

    paraphrases: list[str]


@dataclass
class CaseResult:
    """One run of one question on one model, scored per field (None = not applicable)."""

    case_id: str
    query_class: str
    question: str
    latency_s: float
    outcome: str
    retries: int = 0
    detail: str | None = None
    matched_alternative: int | None = None
    scores: dict[str, bool | None] = field(default_factory=dict)
    misses: list[tuple[str, Any, Any]] = field(default_factory=list)


@dataclass
class CaseRuns:
    """Every repeat of one case on one model."""

    case: dict[str, Any]
    runs: list[CaseResult]

    @property
    def passes(self) -> int:
        """Runs with every field correct."""
        return sum(not run.misses for run in self.runs)

    @property
    def stable(self) -> bool:
        """True when every repeat passed."""
        return self.passes == len(self.runs)


def load_cases(path: Path) -> list[dict[str, Any]]:
    """The eval cases, in file order."""
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def expectations(case: dict[str, Any]) -> list[dict[str, Any]]:
    """The case's acceptable expectations: `expect_any`, or `expect` alone."""
    return case.get("expect_any") or [case["expect"]]


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


def score(
    outcome: str,
    expect: dict[str, Any],
    query_plan: QueryPlan | None,
    unhandled_terms: list[str],
    ranking_direction: str | None = None,
) -> tuple[dict[str, bool | None], list[tuple[str, Any, Any]]]:
    """Score one answer against one expectation: (field -> match or None, misses).

    top_n and bottom_n are always scored (absent means null). unhandled_terms and the
    draft's ranking_direction are scored only for expectations that list them;
    unhandled_terms as an exact set (casefolded).
    """
    scores: dict[str, bool | None] = dict.fromkeys(FIELDS)
    misses: list[tuple[str, Any, Any]] = []

    def check(name: str, expected: Any, actual: Any) -> None:
        matched = expected == actual
        scores[name] = matched
        if not matched:
            misses.append((name, expected, actual))

    check("outcome", expect["outcome"], outcome)
    if expect["outcome"] != "plan" or query_plan is None:
        return scores, misses
    check("cohort_count", len(expect["cohorts"]), len(query_plan.cohorts))
    check("filters", expected_filter_sets(expect), cohort_filter_sets(query_plan))
    expected_grouping = expect.get("dimension") or expect.get("relation")
    check("grouping", expected_grouping, query_plan.dimension or query_plan.relation)
    expected_unit = granularity(expect.get("dimension"))
    if expected_unit is not None:
        check("granularity", expected_unit, granularity(query_plan.dimension))
    check("top_n", expect.get("top_n"), query_plan.top_n)
    check("bottom_n", expect.get("bottom_n"), query_plan.bottom_n)
    if "ranking_direction" in expect:
        check("ranking_direction", expect["ranking_direction"], ranking_direction)
    if "unhandled_terms" in expect:
        check(
            "unhandled_terms",
            sorted(term.casefold() for term in expect["unhandled_terms"]),
            sorted(term.casefold() for term in unhandled_terms),
        )
    return scores, misses


def score_best(
    result: CaseResult,
    case: dict[str, Any],
    query_plan: QueryPlan | None,
    unhandled_terms: list[str],
    ranking_direction: str | None,
) -> None:
    """Score against every acceptable expectation and keep the best match.

    The first expectation with no misses wins; otherwise the one with the fewest misses.
    `matched_alternative` is set only for `expect_any` cases that passed.
    """
    scored = [
        score(result.outcome, expect, query_plan, unhandled_terms, ranking_direction)
        for expect in expectations(case)
    ]
    best = min(range(len(scored)), key=lambda index: len(scored[index][1]))
    result.scores, result.misses = scored[best]
    if "expect_any" in case and not result.misses:
        result.matched_alternative = best


def plan_summary(
    query_plan: QueryPlan, unhandled_terms: list[str], ranking_direction: str | None
) -> str:
    """A one-line account of what the planner produced, for the misses list."""
    cohorts = [normalized_filters(c.filters.model_dump(mode="json")) for c in query_plan.cohorts]
    parts = [
        f"cohorts={cohorts}",
        f"dimension={query_plan.dimension}",
        f"relation={query_plan.relation}",
        f"top_n={query_plan.top_n}",
        f"bottom_n={query_plan.bottom_n}",
        f"ranking_direction={ranking_direction}",
    ]
    if unhandled_terms:
        parts.append(f"unhandled_terms={unhandled_terms}")
    return "plan " + ", ".join(parts)


async def run_case(
    llm: LlmClient, case: dict[str, Any], today: date, default_top_n_for_ranking: int
) -> CaseResult:
    """Plan one question once and score it."""
    started = time.perf_counter()
    query_plan: QueryPlan | None = None
    unhandled_terms: list[str] = []
    ranking_direction: str | None = None
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
        unhandled_terms = interpretation.unhandled_terms
        if interpretation.draft is not None and interpretation.draft.ranking_direction:
            ranking_direction = interpretation.draft.ranking_direction.value
        detail = plan_summary(query_plan, unhandled_terms, ranking_direction)
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
    score_best(result, case, query_plan, unhandled_terms, ranking_direction)
    return result


async def run_repeats(
    llm: LlmClient,
    cases: list[dict[str, Any]],
    repeats: int,
    today: date,
    default_top_n_for_ranking: int,
    concurrency: int,
) -> list[CaseRuns]:
    """Run every case `repeats` times, at most `concurrency` planner calls at once."""
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case: dict[str, Any]) -> CaseResult:
        async with semaphore:
            return await run_case(llm, case, today, default_top_n_for_ranking)

    runs = await asyncio.gather(*(one(case) for case in cases for _ in range(repeats)))
    return [
        CaseRuns(case=case, runs=list(runs[index * repeats : (index + 1) * repeats]))
        for index, case in enumerate(cases)
    ]


def load_paraphrase_cache(path: Path) -> dict[str, dict[str, Any]]:
    """Cached paraphrase entries by seed id; empty when the file does not exist."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        entries = yaml.safe_load(handle) or []
    return {entry["seed_id"]: entry for entry in entries}


async def generate_paraphrases(llm: LlmClient, question: str, count: int) -> list[str]:
    """Ask the LLM for `count` rewrites of `question`.

    Raises ValueError when the LLM returns fewer distinct rewrites than asked for.
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": PARAPHRASE_INSTRUCTIONS.format(count=count)},
        {"role": "user", "content": question},
    ]
    answer = await llm.structured_plan(messages, Paraphrases)
    rewrites: list[str] = []
    for rewrite in answer.value.paraphrases:
        stripped = rewrite.strip()
        if stripped and stripped != question and stripped not in rewrites:
            rewrites.append(stripped)
    if len(rewrites) < count:
        raise ValueError(
            f"The LLM returned {len(rewrites)} distinct paraphrases of {question!r}, "
            f"expected {count}; re-run to try again."
        )
    return rewrites[:count]


async def paraphrase_cases(
    llm: LlmClient, seeds: list[dict[str, Any]], count: int, path: Path
) -> list[dict[str, Any]]:
    """Paraphrase cases for `seeds`, generating only those missing from the cache.

    A cache entry is reused when its seed question is unchanged and it holds at least
    `count` paraphrases; otherwise it is regenerated and the cache file rewritten. Each
    paraphrase case inherits the seed's class and expectations.
    """
    cache = load_paraphrase_cache(path)
    changed = False
    for seed in seeds:
        entry = cache.get(seed["id"])
        if (
            entry is None
            or entry["seed_question"] != seed["question"]
            or len(entry["paraphrases"]) < count
        ):
            logger.info("Paraphrasing %s with %s", seed["id"], llm.model)
            cache[seed["id"]] = {
                "seed_id": seed["id"],
                "seed_question": seed["question"],
                "model": llm.model,
                "generated_on": date.today().isoformat(),
                "paraphrases": await generate_paraphrases(llm, seed["question"], count),
            }
            changed = True
    if changed:
        write_paraphrase_cache(path, cache)
    cases: list[dict[str, Any]] = []
    for seed in seeds:
        for index, question in enumerate(cache[seed["id"]]["paraphrases"][:count], start=1):
            case = {key: value for key, value in seed.items() if key != "note"}
            cases.append({**case, "id": f"{seed['id']}~p{index}", "question": question})
    return cases


def write_paraphrase_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    """Write the cache with a header saying how it was produced."""
    header = (
        "# Paraphrases of the seed questions in questions.yaml, generated by the configured\n"
        "# LLM (scripts/eval_planner.py --paraphrases K) and cached so every run scores the\n"
        "# same text. Each paraphrase is scored against its seed's expectations. Delete an\n"
        "# entry to regenerate it; an entry whose seed question changed is regenerated.\n"
    )
    body = yaml.safe_dump(list(cache.values()), allow_unicode=True, sort_keys=False, width=100)
    path.write_text(header + body, encoding="utf-8")


def field_accuracy(results: list[CaseResult]) -> dict[str, tuple[int, int]]:
    """Field -> (matches, applicable runs)."""
    accuracy: dict[str, tuple[int, int]] = {}
    for name in FIELDS:
        applicable = [r.scores[name] for r in results if r.scores.get(name) is not None]
        accuracy[name] = (sum(bool(s) for s in applicable), len(applicable))
    return accuracy


def all_runs(case_runs: list[CaseRuns]) -> list[CaseResult]:
    """Every individual run, flattened."""
    return [run for runs in case_runs for run in runs.runs]


def ratio(hits: int, total: int) -> str:
    """'hits/total (pct%)', or '-' for an empty set."""
    return f"{hits}/{total} ({100 * hits / total:.0f}%)" if total else "-"


def provenance() -> tuple[str, str]:
    """(commit hash with a dirty-tree marker, short SHA-256 of planner/prompt.py)."""
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    if status.strip():
        commit += " (working tree has uncommitted changes)"
    prompt_hash = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]
    return commit, prompt_hash


def summary_rows(by_model: dict[str, dict[str, list[CaseRuns]]]) -> list[str]:
    """The summary table: stable pass rate per set, per-run pass rate and latency."""
    lines = [
        "| model | seed cases stable | paraphrase cases stable | stable pass rate (all) "
        "| per-run pass rate | mean latency | p50 | max | retries |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for model, sets in by_model.items():
        seeds, paraphrases = sets["seed"], sets["paraphrase"]
        everything = seeds + paraphrases
        runs = all_runs(everything)
        latencies = [r.latency_s for r in runs]
        lines.append(
            f"| `{model}` | {ratio(sum(c.stable for c in seeds), len(seeds))} | "
            f"{ratio(sum(c.stable for c in paraphrases), len(paraphrases))} | "
            f"{ratio(sum(c.stable for c in everything), len(everything))} | "
            f"{ratio(sum(not r.misses for r in runs), len(runs))} | "
            f"{statistics.mean(latencies):.1f}s | {statistics.median(latencies):.1f}s | "
            f"{max(latencies):.1f}s | {sum(r.retries for r in runs)} |"
        )
    return lines


def field_rows(by_model: dict[str, dict[str, list[CaseRuns]]]) -> list[str]:
    """Per-field accuracy over every run, one row per model and case set."""
    lines = [
        "| model | set | " + " | ".join(FIELDS) + " |",
        "|---|---|" + "---|" * len(FIELDS),
    ]
    for model, sets in by_model.items():
        for set_name, case_runs in sets.items():
            if not case_runs:
                continue
            accuracy = field_accuracy(all_runs(case_runs))
            cells = [f"{hits}/{total}" if total else "-" for hits, total in accuracy.values()]
            lines.append(f"| `{model}` | {set_name} | " + " | ".join(cells) + " |")
    return lines


def case_rows(by_model: dict[str, dict[str, list[CaseRuns]]], set_name: str) -> list[str]:
    """Per-case pass counts and mean latency for one case set, all models side by side."""
    models = list(by_model)
    first = by_model[models[0]][set_name]
    lines = [
        "| id | class | " + " | ".join(f"`{m}`" for m in models) + " |",
        "|---|---|" + "---|" * len(models),
    ]
    for index, case_runs in enumerate(first):
        cells = []
        for model in models:
            runs = by_model[model][set_name][index]
            latency = statistics.mean(r.latency_s for r in runs.runs)
            mark = "" if runs.stable else " **miss**"
            cells.append(f"passed {runs.passes}/{len(runs.runs)}{mark} ({latency:.1f}s)")
        lines.append(f"| {case_runs.case['id']} | {case_runs.case['class']} | " + " | ".join(cells))
        lines[-1] += " |"
    return lines


def alternative_rows(by_model: dict[str, dict[str, list[CaseRuns]]]) -> list[str]:
    """Which acceptable expectation each passing run of an `expect_any` case matched."""
    lines: list[str] = []
    for model, sets in by_model.items():
        for case_runs in sets["seed"] + sets["paraphrase"]:
            if "expect_any" not in case_runs.case:
                continue
            outcomes = [
                "miss"
                if run.matched_alternative is None
                else case_runs.case["expect_any"][run.matched_alternative]["outcome"]
                for run in case_runs.runs
            ]
            lines.append(
                f"- `{model}`, **{case_runs.case['id']}**: runs answered as "
                + ", ".join(f"`{o}`" for o in outcomes)
            )
    return lines


def miss_lines(by_model: dict[str, dict[str, list[CaseRuns]]]) -> list[str]:
    """Every failing run with its field diffs and what the planner said."""
    lines: list[str] = []
    for model, sets in by_model.items():
        lines += [f"### `{model}`", ""]
        missed = [c for c in sets["seed"] + sets["paraphrase"] if not c.stable]
        if not missed:
            lines.append("No misses.")
        for case_runs in missed:
            lines.append(
                f"- **{case_runs.case['id']}** (passed {case_runs.passes}/"
                f"{len(case_runs.runs)}): {case_runs.case['question']}"
            )
            for number, run in enumerate(case_runs.runs, start=1):
                if not run.misses:
                    continue
                diffs = "; ".join(
                    f"`{name}` expected `{expected}`, got `{actual}`"
                    for name, expected, actual in run.misses
                )
                lines.append(f"  - run {number}: {diffs}")
                if run.detail:
                    lines.append(f"    - planner said: {run.detail}")
        lines.append("")
    return lines


def note_lines(seeds: list[CaseRuns]) -> list[str]:
    """The `note` of every seed case that has one."""
    return [
        f"- **{c.case['id']}**: {' '.join(c.case['note'].split())}"
        for c in seeds
        if c.case.get("note")
    ]


def limitations(seed_count: int, paraphrase_count: int) -> str:
    """The Limitations section, sized to the case counts of this run."""
    points = round(100 / seed_count) if seed_count else 0
    text = f"""\
## Limitations

- {seed_count} seed questions detect systematic failures (a query class the planner cannot
  map, a rule it ignores), not statistically robust accuracy: one miss moves a field by
  about {points} points, and a perfect score does not mean the planner is always right.
- Results are for one prompt version on one day. Model outputs vary between runs at the
  same settings, providers update models behind the same name, and any change to the
  prompt, few-shot examples or registry descriptions needs a new run.
- Questions were written by the developer of the prompt, so they share its vocabulary;
  real users phrase questions differently and misspell names.
- Latency is wall-clock time from one machine, including the network and any retry."""
    if paraphrase_count:
        text += """
- The paraphrase cases were generated by an LLM (the configured planner model), not by
  real users, and each inherits its seed's expectations unchecked: a paraphrase that
  drifted in meaning would be scored against the wrong answer. They test robustness to
  rewording, not to the vocabulary or mistakes of real users."""
    return text


def render_report(
    by_model: dict[str, dict[str, list[CaseRuns]]],
    today: date,
    repeats: int,
    concurrency: int,
    paraphrase_model: str | None,
) -> str:
    """Markdown report: provenance, summary, per-field and per-case tables, misses."""
    commit, prompt_hash = provenance()
    sets = next(iter(by_model.values()))
    seed_count, paraphrase_count = len(sets["seed"]), len(sets["paraphrase"])
    lines = [
        "# Planner eval",
        "",
        f"- Date: {today.isoformat()}",
        f"- Commit: `{commit}`",
        f"- Prompt file: `src/ctviz/planner/prompt.py`, SHA-256 `{prompt_hash}…`",
        f"- Cases: {seed_count} seed questions from `tests/eval/questions.yaml`"
        + (
            f" and {paraphrase_count} paraphrases from `tests/eval/paraphrases.yaml` "
            f"(generated by `{paraphrase_model}`)"
            if paraphrase_count
            else ""
        ),
        f"- Repeats: every case was planned {repeats} time(s) per model. A case is "
        "*stable* when every repeat got every scored field right.",
        f"- Concurrency: at most {concurrency} planner call(s) in flight at once.",
        "",
        "Each question goes through the real planner (prompt, merge, validation, one retry).",
        "",
        "## Summary",
        "",
        *summary_rows(by_model),
        "",
        "## Per-field accuracy (all runs)",
        "",
        *field_rows(by_model),
        "",
        "## Seed cases",
        "",
        *case_rows(by_model, "seed"),
        "",
    ]
    if paraphrase_count:
        lines += ["## Paraphrase cases", "", *case_rows(by_model, "paraphrase"), ""]
    alternatives = alternative_rows(by_model)
    if alternatives:
        lines += ["## Cases with several acceptable answers", "", *alternatives, ""]
    notes = note_lines(sets["seed"])
    if notes:
        lines += ["## Notes on cases", "", *notes, ""]
    lines += ["## Misses", "", *miss_lines(by_model)]
    lines.append(limitations(seed_count, paraphrase_count))
    return "\n".join(lines)


def llm_for(settings: Settings, model: str) -> LlmClient:
    """An OpenAI-only chain for `model`, so each model is scored without fallback."""
    llm = build_llm(settings.model_copy(update={"llm_fallback_chain": f"openai:{model}"}))
    if llm is None:
        sys.exit("OPENAI_API_KEY is not set; the eval needs a real LLM.")
    return llm


def configured_llm(settings: Settings) -> LlmClient:
    """The configured fallback chain (LLM_FALLBACK_CHAIN), used to write paraphrases."""
    llm = build_llm(settings)
    if llm is None:
        sys.exit("No LLM provider is configured; paraphrases need a real LLM.")
    return llm


async def main() -> None:
    """Run every case on every model and print (and optionally write) the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models", nargs="+", help="OpenAI models; defaults to the first in LLM_FALLBACK_CHAIN."
    )
    parser.add_argument("--ids", nargs="+", help="Only run the cases with these seed ids.")
    parser.add_argument(
        "--repeats", type=int, default=1, help="Times each case is planned per model."
    )
    parser.add_argument(
        "--paraphrases",
        type=int,
        default=0,
        help="Also score K cached LLM paraphrases per seed question (generated if missing).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=1, help="Planner calls in flight at once."
    )
    parser.add_argument("--output", type=Path, help="Write the Markdown report here.")
    args = parser.parse_args()
    if args.repeats < 1 or args.paraphrases < 0 or args.concurrency < 1:
        parser.error("--repeats and --concurrency must be >= 1, --paraphrases >= 0.")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    models = args.models or [parse_chain(settings.llm_fallback_chain)[0][1]]
    seeds = [c for c in load_cases(QUESTIONS_PATH) if not args.ids or c["id"] in args.ids]
    paraphrase_model: str | None = None
    paraphrases: list[dict[str, Any]] = []
    if args.paraphrases:
        generator = configured_llm(settings)
        paraphrase_model = generator.model
        paraphrases = await paraphrase_cases(generator, seeds, args.paraphrases, PARAPHRASES_PATH)
    today = date.today()
    by_model: dict[str, dict[str, list[CaseRuns]]] = {}
    for model in models:
        llm = llm_for(settings, model)
        logger.info(
            "Evaluating %s on %d cases x %d repeats",
            model,
            len(seeds) + len(paraphrases),
            args.repeats,
        )
        by_model[model] = {
            set_name: await run_repeats(
                llm,
                cases,
                args.repeats,
                today,
                settings.default_top_n_for_ranking,
                args.concurrency,
            )
            for set_name, cases in (("seed", seeds), ("paraphrase", paraphrases))
        }
    report = render_report(by_model, today, args.repeats, args.concurrency, paraphrase_model)
    sys.stdout.write(report + "\n")
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
        logger.info("Wrote %s", args.output)


if __name__ == "__main__":
    asyncio.run(main())
