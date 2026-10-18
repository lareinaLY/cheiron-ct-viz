"""FastAPI app: POST /query and GET /health.

Run: uv run uvicorn ctviz.api.app:app --reload
Every failure is returned as an ErrorResponse (code, message, suggestions) instead of a
chart (invariant 7).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ctviz import pipeline
from ctviz.api.request import QueryRequest
from ctviz.config import Settings
from ctviz.errors import (
    ApiRequestError,
    InvalidFilterError,
    PlanErrorReason,
    PlanValidationError,
    RateLimitError,
    UpstreamError,
)
from ctviz.retrieval.cache import build_cache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import ChartType, Dimension, Relation
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
    PlanErrorReason.INVALID_TOP_N: ["Use a `top_n` of 1 or more, or omit it."],
    PlanErrorReason.TOP_N_REQUIRES_DIMENSION: ["Remove `top_n` for network queries."],
    PlanErrorReason.INVALID_YEAR_RANGE: ["Make `start_year_from` <= `start_year_to`."],
}

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        422: "The request or plan cannot be answered faithfully as written.",
        502: "ClinicalTrials.gov failed or rejected the generated query.",
        503: "ClinicalTrials.gov is rate-limiting this server; retry later.",
    }.items()
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; the API client lives for the app's lifetime (shared pacing)."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with CtGovClient(settings, cache=build_cache(settings)) as client:
            app.state.client = client
            yield

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
        body: QueryRequest, client: Annotated[CtGovClient, Depends(get_client)]
    ) -> VisualizationResponse:
        """Answer a question from its structured fields with a cited chart spec."""
        return await pipeline.run(body.to_plan(), question=body.query, client=client)

    return app


def get_client(request: Request) -> CtGovClient:
    """The app-wide API client created in the lifespan."""
    return request.app.state.client


def error_response(status: int, code: str, message: str, suggestions: list[str]) -> JSONResponse:
    """Serialize an ErrorResponse."""
    body = ErrorResponse(code=code, message=message, suggestions=suggestions)
    return JSONResponse(status_code=status, content=body.model_dump())


def register_error_handlers(app: FastAPI) -> None:
    """Map domain and retrieval exceptions to structured error bodies."""

    @app.exception_handler(PlanValidationError)
    async def plan_error(request: Request, error: PlanValidationError) -> JSONResponse:
        return error_response(422, error.reason.value, str(error), PLAN_SUGGESTIONS[error.reason])

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
