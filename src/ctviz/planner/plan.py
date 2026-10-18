"""Planner entry point: a request becomes a validated QueryPlan plus how it was made.

With an LLM, the question is drafted into an `LlmPlanDraft`, explicit request fields
are applied on top, and the result is validated; one rejected draft is retried with
the validation error. Without an LLM, only the structured fields are used. The LLM
never produces data values (invariant 1): a draft only names filters and a grouping.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from openai.types.chat import ChatCompletionMessageParam
from pydantic import TypeAdapter, ValidationError

from ctviz.aggregate.registry import DIMENSIONS, RELATIONS
from ctviz.config import DEFAULT_TOP_N_FOR_RANKING
from ctviz.errors import (
    InvalidFilterError,
    PlannerError,
    PlannerErrorCode,
    PlanValidationError,
)
from ctviz.planner.llm import LlmCallLog, LlmClient, answer_call, error_class, record_call
from ctviz.planner.prompt import build_messages, build_system_prompt
from ctviz.planner.validation import validate_plan
from ctviz.retrieval.query_builder import build_params
from ctviz.schemas.llm import LlmCallOutcome
from ctviz.schemas.plan import Cohort, Filters, LlmPlanDraft, QueryPlan, RankingDirection
from ctviz.schemas.request import QueryRequest, cohort_label
from ctviz.schemas.response import FieldOverride, Interpretation

logger = logging.getLogger(__name__)

# One draft plus one retry with the validation error; more rarely helps and doubles cost.
MAX_ATTEMPTS = 2

FILTER_FIELDS = tuple(Filters.model_fields)
PLAN_FIELDS = ("dimension", "relation", "chart_hint", "top_n", "match_mode")

JSON = TypeAdapter(Any)


async def plan(
    request: QueryRequest,
    llm: LlmClient | None,
    *,
    today: date,
    default_top_n_for_ranking: int = DEFAULT_TOP_N_FOR_RANKING,
    call_log: LlmCallLog | None = None,
) -> Interpretation:
    """Turn a request into a validated plan and record how it was planned.

    `default_top_n_for_ranking` is the top_n used when the LLM flags a ranking question
    ("which countries have the most ...") that gives no number. Every LLM call made is
    appended to `call_log` when one is given (for meta.llm).

    Raises PlannerError when no plan can be made (no LLM and no structured grouping, an
    unsupported or ambiguous question, an LLM failure, or two invalid drafts), and
    PlanValidationError or InvalidFilterError when structured fields alone are invalid.
    """
    started = time.perf_counter()
    if llm is None:
        interpretation = plan_by_rules(request)
    else:
        interpretation = await plan_by_llm(
            request, llm, today, default_top_n_for_ranking, call_log or LlmCallLog()
        )
    interpretation.planning_ms = round((time.perf_counter() - started) * 1000, 1)
    return interpretation


def plan_by_rules(request: QueryRequest) -> Interpretation:
    """Plan from structured fields only; the question text is not read.

    A request with no structured fields at all needs interpretation, which is impossible
    without an LLM. A request with some structured fields is validated as written, so a
    missing grouping is reported as the plan error it is.
    """
    if not request.explicit_fields():
        raise PlannerError(
            PlannerErrorCode.LLM_NOT_CONFIGURED,
            "No LLM planner is configured (no provider in LLM_FALLBACK_CHAIN has an API "
            "key, e.g. OPENAI_API_KEY), so a question without structured fields cannot be "
            "interpreted.",
            [
                f"Send `dimension` (one of: {', '.join(d.value for d in DIMENSIONS)}) "
                "with filters such as `drug` or `condition`.",
                f"Or send `relation` (one of: {', '.join(r.value for r in RELATIONS)}).",
                "Or configure OPENAI_API_KEY on the server.",
            ],
        )
    query_plan = request.to_plan()
    check_plan(query_plan)
    return Interpretation(question=request.query, plan=query_plan, planned_by="rules")


async def plan_by_llm(
    request: QueryRequest,
    llm: LlmClient,
    today: date,
    default_top_n_for_ranking: int,
    call_log: LlmCallLog,
) -> Interpretation:
    """Draft with the LLM, merge explicit fields, validate; retry once on rejection.

    Explicit filter values are checked first: they override every draft, so asking the
    LLM again could not fix them, and the caller should get the filter error itself.
    """
    build_params(request.filters(), request.match_mode)
    messages = build_messages(request.query, today)
    rejected: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        answer = await llm.structured_plan(messages, LlmPlanDraft)
        draft = answer.value
        try:
            raise_if_unanswerable(draft, today)
        except PlannerError:
            # Declining or asking back is a valid answer, not a failed call.
            call_log.add_answer(answer, record_call(answer_call(answer, LlmCallOutcome.OK)))
            raise
        try:
            query_plan, overrides = merge_draft(draft, request)
            query_plan, ranking_top_n = apply_ranking_default(
                query_plan, draft, request, default_top_n_for_ranking
            )
            check_plan(query_plan)
        except (PlanValidationError, InvalidFilterError, ValidationError) as error:
            problem = describe_error(error)
            rejected.append(problem)
            outcome = (
                LlmCallOutcome.VALIDATION_RETRY if attempt < MAX_ATTEMPTS else LlmCallOutcome.FAILED
            )
            call_log.add_answer(
                answer, record_call(answer_call(answer, outcome, error_class(error)))
            )
            messages = [*messages, *retry_messages(draft, problem, request)]
            continue
        call_log.add_answer(answer, record_call(answer_call(answer, LlmCallOutcome.OK)))
        return Interpretation(
            question=request.query,
            plan=query_plan,
            planned_by="llm",
            model=llm.model,
            provider=answer.provider,
            model_used=answer.model,
            response_mode=answer.response_mode,
            fallback_reason=answer.fallback_reason,
            draft=draft,
            overrides=overrides,
            rejected_attempts=rejected,
            ranking_top_n=ranking_top_n,
            unhandled_terms=clean_terms(draft.unhandled_terms),
        )
    raise PlannerError(
        PlannerErrorCode.PLANNING_FAILED,
        f"The LLM planner could not produce a valid plan in {MAX_ATTEMPTS} attempts. "
        f"Last problem: {rejected[-1]}",
        [
            "Rephrase the question with one grouping (e.g. 'by phase', 'per year').",
            "Or send structured fields (`drug`, `condition`, `dimension`, ...).",
        ],
    )


# The LLM's unsupported_reason is free text shown to the user, so a prompt-injection
# question can make it repeat the system prompt (live adversarial test, 2026-10-18).
MAX_UNSUPPORTED_REASON_LENGTH = 240
PROMPT_ECHO_WINDOW = 30
REDACTED_UNSUPPORTED_REASON = (
    "The question asks for something this service does not do (it only builds charts "
    "from ClinicalTrials.gov registry data)."
)


def normalized(text: str) -> str:
    """Lowercase with whitespace runs collapsed to one space, for echo detection."""
    return " ".join(text.lower().split())


def echoes_prompt(reason: str, system_prompt: str) -> bool:
    """True when any PROMPT_ECHO_WINDOW-character run of `reason` occurs in the prompt."""
    text, prompt = normalized(reason), normalized(system_prompt)
    return any(
        text[start : start + PROMPT_ECHO_WINDOW] in prompt
        for start in range(len(text) - PROMPT_ECHO_WINDOW + 1)
    )


def redact_unsupported_reason(reason: str, system_prompt: str) -> str:
    """The reason safe to show: a fixed sentence if it quotes the prompt, else at most 240 chars.

    The echo check runs on the whole reason, so prompt text after the cut is caught too.
    """
    if echoes_prompt(reason, system_prompt):
        return REDACTED_UNSUPPORTED_REASON
    if len(reason) <= MAX_UNSUPPORTED_REASON_LENGTH:
        return reason
    return reason[: MAX_UNSUPPORTED_REASON_LENGTH - 1] + "…"


def raise_if_unanswerable(draft: LlmPlanDraft, today: date) -> None:
    """Turn the LLM's 'cannot answer' or 'which reading?' into a structured error.

    `today` rebuilds the system prompt the draft was made with, to redact echoes of it.
    """
    if draft.unsupported_reason:
        reason = redact_unsupported_reason(draft.unsupported_reason, build_system_prompt(today))
        if reason == REDACTED_UNSUPPORTED_REASON:
            logger.warning(
                "LLM unsupported_reason quoted the system prompt and was replaced "
                "(code=unsupported_reason_redacted)"
            )
        raise PlannerError(
            PlannerErrorCode.UNSUPPORTED_QUESTION,
            f"This question cannot be answered from ClinicalTrials.gov trial counts: {reason}",
            [
                "Ask for numbers of trials grouped by one of: "
                + ", ".join(spec.label for spec in DIMENSIONS.values())
                + ".",
                "Or for a network: " + ", ".join(spec.label for spec in RELATIONS.values()) + ".",
            ],
        )
    if draft.clarification is not None:
        raise PlannerError(
            PlannerErrorCode.CLARIFICATION_NEEDED,
            f"The question can be read in more than one way: {draft.clarification.question}",
            list(draft.clarification.options),
        )


def merge_draft(
    draft: LlmPlanDraft, request: QueryRequest
) -> tuple[QueryPlan, list[FieldOverride]]:
    """Apply the request's explicit fields on top of the draft.

    Explicit filters apply to every cohort. An explicit dimension clears the draft's
    relation and vice versa, so the caller's choice is never rejected as "both set".
    Cohort labels are derived from the merged filters, never taken from the LLM.
    Raises pydantic ValidationError when the draft has no cohorts.
    """
    explicit = request.explicit_fields()
    overrides: list[FieldOverride] = []
    cohorts: list[Cohort] = []
    for index, filters in enumerate(draft.cohorts):
        update: dict[str, Any] = {}
        for name in FILTER_FIELDS:
            if name in explicit:
                update[name] = getattr(request, name)
                record_override(
                    overrides,
                    f"cohorts[{index}].filters.{name}",
                    getattr(filters, name),
                    update[name],
                )
        merged = filters.model_copy(update=update)
        cohorts.append(Cohort(label=cohort_label(merged), filters=merged))

    values: dict[str, Any] = {name: getattr(draft, name) for name in PLAN_FIELDS}
    for name in PLAN_FIELDS:
        if name in explicit:
            values[name] = getattr(request, name)
    if request.dimension is not None and "relation" not in explicit:
        values["relation"] = None
    if request.relation is not None and "dimension" not in explicit:
        values["dimension"] = None
    # The draft has no bottom_n (its top_n plus ranking_direction express "fewest"), so an
    # explicit bottom_n is the caller's ranking and clears the draft's top_n.
    bottom_n = request.bottom_n if "bottom_n" in explicit else None
    if bottom_n is not None and "top_n" not in explicit:
        values["top_n"] = None
    for name in PLAN_FIELDS:
        record_override(overrides, name, getattr(draft, name), values[name])
    record_override(overrides, "bottom_n", None, bottom_n)
    return QueryPlan(cohorts=cohorts, bottom_n=bottom_n, **values), overrides


def apply_ranking_default(
    query_plan: QueryPlan, draft: LlmPlanDraft, request: QueryRequest, default_top_n: int
) -> tuple[QueryPlan, int | None]:
    """Turn the draft's ranking direction into top_n or bottom_n.

    The LLM only flags the direction; the number is chosen here (invariant 1):
    - "most": top_n, from the question if it gave a number, else the configured default.
    - "fewest": bottom_n, likewise. The draft's top_n is the number the user gave, so
      with "fewest" it moves to bottom_n. A `top_n` sent in the request is an explicit
      "top" and wins: the plan keeps it and gets no bottom_n. A `bottom_n` sent in the
      request wins over either direction the same way.
    Networks are never trimmed (the validator rejects top_n/bottom_n with a relation).
    Returns the plan and the default applied, or None when no default was used.
    """
    direction = draft.ranking_direction
    if direction is None or query_plan.dimension is None or "bottom_n" in request.explicit_fields():
        return query_plan, None
    if direction == RankingDirection.MOST:
        if query_plan.top_n is not None:
            return query_plan, None
        return query_plan.model_copy(update={"top_n": default_top_n}), default_top_n
    if "top_n" in request.explicit_fields():
        return query_plan, None
    if query_plan.top_n is not None:
        return query_plan.model_copy(update={"top_n": None, "bottom_n": query_plan.top_n}), None
    return query_plan.model_copy(update={"bottom_n": default_top_n}), default_top_n


def clean_terms(terms: list[str]) -> list[str]:
    """The LLM's unhandled terms, stripped, without blanks and case-insensitive repeats."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for term in terms:
        stripped = term.strip()
        if stripped and stripped.casefold() not in seen:
            seen.add(stripped.casefold())
            cleaned.append(stripped)
    return cleaned


def record_override(
    overrides: list[FieldOverride], path: str, llm_value: Any, final_value: Any
) -> None:
    """Record a field whose final value differs from the LLM's."""
    if llm_value != final_value:
        overrides.append(
            FieldOverride(
                path=path,
                llm_value=JSON.dump_python(llm_value, mode="json"),
                explicit_value=JSON.dump_python(final_value, mode="json"),
            )
        )


def check_plan(query_plan: QueryPlan) -> None:
    """Validate the plan and every cohort's filter values, as the pipeline will."""
    validate_plan(query_plan)
    for cohort in query_plan.cohorts:
        build_params(cohort.filters, query_plan.match_mode)


def describe_error(error: Exception) -> str:
    """A one-line description of why a draft was rejected."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'plan'}: {item['msg']}"
            for item in error.errors()
        )
    return str(error)


def retry_messages(
    draft: LlmPlanDraft, problem: str, request: QueryRequest
) -> list[ChatCompletionMessageParam]:
    """The rejected draft and the reason, so the next draft can fix it."""
    explicit = sorted(request.explicit_fields())
    pinned = (
        f" These fields were set explicitly by the user and override yours: {', '.join(explicit)}."
        if explicit
        else ""
    )
    return [
        {"role": "assistant", "content": draft.model_dump_json()},
        {
            "role": "user",
            "content": f"That plan was rejected: {problem}.{pinned} Return a corrected plan "
            "for the same question, or set unsupported_reason if it cannot be answered.",
        },
    ]
