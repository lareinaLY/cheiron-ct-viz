"""Planner entry point: a request becomes a validated QueryPlan plus how it was made.

With an LLM, the question is drafted into an `LlmPlanDraft`, explicit request fields
are applied on top, and the result is validated; one rejected draft is retried with
the validation error. Without an LLM, only the structured fields are used. The LLM
never produces data values (invariant 1): a draft only names filters and a grouping.
"""

from __future__ import annotations

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
from ctviz.planner.llm import LlmCallOutcome, LlmClient, log_llm_answer
from ctviz.planner.prompt import build_messages
from ctviz.planner.validation import validate_plan
from ctviz.retrieval.query_builder import build_params
from ctviz.schemas.plan import Cohort, Filters, LlmPlanDraft, QueryPlan
from ctviz.schemas.request import QueryRequest, cohort_label
from ctviz.schemas.response import FieldOverride, Interpretation

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
) -> Interpretation:
    """Turn a request into a validated plan and record how it was planned.

    `default_top_n_for_ranking` is the top_n used when the LLM flags a ranking question
    ("which countries have the most ...") that gives no number.

    Raises PlannerError when no plan can be made (no LLM and no structured grouping, an
    unsupported or ambiguous question, an LLM failure, or two invalid drafts), and
    PlanValidationError or InvalidFilterError when structured fields alone are invalid.
    """
    started = time.perf_counter()
    if llm is None:
        interpretation = plan_by_rules(request)
    else:
        interpretation = await plan_by_llm(request, llm, today, default_top_n_for_ranking)
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
    request: QueryRequest, llm: LlmClient, today: date, default_top_n_for_ranking: int
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
            raise_if_unanswerable(draft)
        except PlannerError:
            # Declining or asking back is a valid answer, not a failed call.
            log_llm_answer(answer, LlmCallOutcome.OK)
            raise
        try:
            query_plan, overrides = merge_draft(draft, request)
            query_plan, ranking_top_n = apply_ranking_default(
                query_plan, draft, default_top_n_for_ranking
            )
            check_plan(query_plan)
        except (PlanValidationError, InvalidFilterError, ValidationError) as error:
            problem = describe_error(error)
            rejected.append(problem)
            log_llm_answer(
                answer,
                LlmCallOutcome.RETRY if attempt < MAX_ATTEMPTS else LlmCallOutcome.FAILED,
            )
            messages = [*messages, *retry_messages(draft, problem, request)]
            continue
        log_llm_answer(answer, LlmCallOutcome.OK)
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


def raise_if_unanswerable(draft: LlmPlanDraft) -> None:
    """Turn the LLM's 'cannot answer' or 'which reading?' into a structured error."""
    if draft.unsupported_reason:
        raise PlannerError(
            PlannerErrorCode.UNSUPPORTED_QUESTION,
            "This question cannot be answered from ClinicalTrials.gov trial counts: "
            f"{draft.unsupported_reason}",
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
    for name in PLAN_FIELDS:
        record_override(overrides, name, getattr(draft, name), values[name])
    return QueryPlan(cohorts=cohorts, **values), overrides


def apply_ranking_default(
    query_plan: QueryPlan, draft: LlmPlanDraft, default_top_n: int
) -> tuple[QueryPlan, int | None]:
    """Set top_n to the configured default for a ranking question that gives no number.

    The LLM only flags the intent; the number is chosen here (invariant 1). A top_n from
    the question or the request wins. Networks are never trimmed (the validator rejects
    top_n with a relation). Returns the plan and the default applied, or None.
    """
    if not draft.ranking_intent or query_plan.top_n is not None or query_plan.dimension is None:
        return query_plan, None
    return query_plan.model_copy(update={"top_n": default_top_n}), default_top_n


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
