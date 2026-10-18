"""FastAPI app: POST /query and GET /health.

Run: uv run uvicorn ctviz.api.app:app --reload
Every failure is returned as an ErrorResponse (code, message, suggestions) instead of a
chart (invariant 7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ctviz import pipeline
from ctviz.config import Settings
from ctviz.errors import (
    ApiRequestError,
    InvalidFilterError,
    PlanErrorReason,
    PlannerError,
    PlannerErrorCode,
    PlanValidationError,
    RateLimitError,
    UpstreamDataFormatError,
    UpstreamError,
)
from ctviz.planner.llm import LlmClient, build_llm
from ctviz.planner.plan import plan
from ctviz.retrieval.cache import build_cache
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.enums import check_enum_drift
from ctviz.schemas.plan import (
    MAX_START_YEAR,
    MAX_TOP_N,
    MIN_START_YEAR,
    ChartType,
    Dimension,
    Relation,
)
from ctviz.schemas.request import QueryRequest
from ctviz.schemas.response import ErrorResponse, VisualizationResponse

logger = logging.getLogger(__name__)

DIMENSION_NAMES = ", ".join(d.value for d in Dimension)
RELATION_NAMES = ", ".join(r.value for r in Relation)

PLAN_SUGGESTIONS: dict[PlanErrorReason, list[str]] = {
    PlanErrorReason.NO_DIMENSION_OR_RELATION: [
        f"Set `dimension` to one of: {DIMENSION_NAMES}.",
        f"Or set `relation` to one of: {RELATION_NAMES}.",
    ],
    PlanErrorReason.BOTH_DIMENSION_AND_RELATION: ["Remove either `dimension` or `relation`."],
    PlanErrorReason.COMPARISON_REQUIRES_DIMENSION: ["Compare cohorts with a `dimension`."],
    PlanErrorReason.CHART_HINT_INCOMPATIBLE: [
        "Remove `chart_hint` to let the server choose a chart.",
        f"Chart types: {', '.join(c.value for c in ChartType)}.",
    ],
    PlanErrorReason.INVALID_TOP_N: [f"Use a `top_n` between 1 and {MAX_TOP_N}, or omit it."],
    PlanErrorReason.TOP_N_REQUIRES_DIMENSION: ["Remove `top_n` for network queries."],
    PlanErrorReason.INVALID_YEAR_RANGE: [
        f"Use start years between {MIN_START_YEAR} and {MAX_START_YEAR}.",
        "Make `start_year_from` <= `start_year_to`.",
    ],
    PlanErrorReason.DUPLICATE_COHORT: ["Give each cohort different filters."],
}

# An LLM provider failure is upstream (502); every other planner error is about the question.
PLANNER_ERROR_STATUS: dict[PlannerErrorCode, int] = {
    PlannerErrorCode.LLM_NOT_CONFIGURED: 422,
    PlannerErrorCode.LLM_UNAVAILABLE: 502,
    PlannerErrorCode.UNSUPPORTED_QUESTION: 422,
    PlannerErrorCode.CLARIFICATION_NEEDED: 422,
    PlannerErrorCode.PLANNING_FAILED: 422,
}

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        422: "The question, request or plan cannot be answered faithfully as written "
        "(including unsupported or ambiguous questions).",
        502: "ClinicalTrials.gov or the LLM provider failed, the API rejected the query, or "
        "too many retrieved records were unreadable to return a trustworthy chart.",
        503: "ClinicalTrials.gov is rate-limiting this server; retry later.",
    }.items()
}


def create_app(settings: Settings | None = None, llm: LlmClient | None = None) -> FastAPI:
    """Build the app; the API client lives for the app's lifetime (shared pacing).

    `llm` defaults to the one configured in settings, built at startup so a malformed
    key stops the server with a clear message (None without an API key: then only
    structured fields are planned). Tests pass a fake.
    The enum drift check (`settings.enum_drift_check`) runs as a background task, so a
    slow or failing /studies/enums never delays startup; it is cancelled at shutdown.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.llm = llm if llm is not None else build_llm(settings)
        async with CtGovClient(settings, cache=build_cache(settings)) as client:
            app.state.client = client
            app.state.enum_drift_task = (
                asyncio.create_task(check_enum_drift(client), name="enum-drift-check")
                if settings.enum_drift_check
                else None
            )
            try:
                yield
            finally:
                await cancel_task(app.state.enum_drift_task)

    app = FastAPI(
        title="ctviz",
        summary="Clinical-trial questions to cited visualization specs (ClinicalTrials.gov v2).",
        lifespan=lifespan,
    )
    register_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe; does not call ClinicalTrials.gov."""
        return {"status": "ok"}

    @app.post("/query", responses=ERROR_RESPONSES)
    async def query(
        body: QueryRequest,
        client: Annotated[CtGovClient, Depends(get_client)],
        llm: Annotated[LlmClient | None, Depends(get_llm)],
    ) -> VisualizationResponse:
        """Answer a question with a cited chart spec.

        `query` alone is enough when the LLM planner is configured; structured fields
        override what the LLM reads from it.
        """
        interpretation = await plan(
            body,
            llm,
            today=date.today(),
            default_top_n_for_ranking=settings.default_top_n_for_ranking,
        )
        return await pipeline.execute(
            interpretation,
            client=client,
            network_limits=settings.network_limits(),
            data_quality_thresholds=settings.data_quality_thresholds(),
            count_route=settings.count_route_policy(),
        )

    return app


async def cancel_task(task: asyncio.Task[object] | None) -> None:
    """Cancel a background task that may still be running, and wait for it to stop."""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def get_client(request: Request) -> CtGovClient:
    """The app-wide API client created in the lifespan."""
    return request.app.state.client


def get_llm(request: Request) -> LlmClient | None:
    """The app-wide LLM planner client, or None when no LLM is configured."""
    return request.app.state.llm


def error_response(status: int, code: str, message: str, suggestions: list[str]) -> JSONResponse:
    """Serialize an ErrorResponse."""
    body = ErrorResponse(code=code, message=message, suggestions=suggestions)
    return JSONResponse(status_code=status, content=body.model_dump())


def register_error_handlers(app: FastAPI) -> None:
    """Map domain and retrieval exceptions to structured error bodies."""

    @app.exception_handler(PlanValidationError)
    async def plan_error(request: Request, error: PlanValidationError) -> JSONResponse:
        return error_response(422, error.reason.value, str(error), PLAN_SUGGESTIONS[error.reason])

    @app.exception_handler(PlannerError)
    async def planner_error(request: Request, error: PlannerError) -> JSONResponse:
        logger.info("Planner could not plan the question: %s: %s", error.code, error)
        return error_response(
            PLANNER_ERROR_STATUS[error.code], error.code.value, str(error), error.suggestions
        )

    @app.exception_handler(InvalidFilterError)
    async def filter_error(request: Request, error: InvalidFilterError) -> JSONResponse:
        return error_response(
            422,
            "invalid_filter_value",
            str(error),
            ["Send plain words only: no quotes, parentheses, brackets or AND/OR/NOT."],
        )

    @app.exception_handler(RequestValidationError)
    async def request_error(request: Request, error: RequestValidationError) -> JSONResponse:
        problems = [
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        ]
        return error_response(422, "invalid_request", "; ".join(problems), problems)

    @app.exception_handler(RateLimitError)
    async def rate_limited(request: Request, error: RateLimitError) -> JSONResponse:
        logger.warning("Rate limited: %s", error)
        return error_response(503, "upstream_rate_limited", str(error), ["Retry in a minute."])

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, error: UpstreamError) -> JSONResponse:
        logger.error("Upstream failure: %s", error)
        return error_response(502, "upstream_unavailable", str(error), ["Retry later."])

    @app.exception_handler(UpstreamDataFormatError)
    async def unreadable_data(request: Request, error: UpstreamDataFormatError) -> JSONResponse:
        return error_response(
            502,
            "upstream_data_format_changed",
            str(error),
            [
                "Retry later; if this persists, ClinicalTrials.gov has likely changed its "
                "record format and the normalizer needs updating.",
                "The skipped records and their reasons are in the server log.",
            ],
        )

    @app.exception_handler(ApiRequestError)
    async def rejected(request: Request, error: ApiRequestError) -> JSONResponse:
        logger.error("Query rejected by ClinicalTrials.gov: %s", error)
        return error_response(
            502,
            "upstream_rejected_query",
            str(error),
            ["Simplify the filters; the API error above names the rejected parameter."],
        )


app = create_app()
