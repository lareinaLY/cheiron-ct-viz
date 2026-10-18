"""A result cut by the record cap is marked partial in data_quality, the title and meta."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from ctviz import pipeline
from ctviz.config import DataQualityThresholds, Settings
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import DataQuality, Interpretation, VisualizationResponse
from tests.factories import make_raw_study, set_path

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
MAX_RECORDS = 3
SAMPLE_NOTE = (
    "ordered by StudyFirstPostDate desc (the most recently registered), not a random sample"
)
TIE_NOTE = "trials sharing the cutoff date are picked in the API's unspecified tie order"


def studies_page(studies: list[Any], total: int) -> httpx.Response:
    return httpx.Response(
        200, json={"studies": studies, "totalCount": total, "nextPageToken": "next"}
    )


def studies(prefix: str, count: int = MAX_RECORDS) -> list[dict[str, Any]]:
    return [make_raw_study(f"NCT{prefix}{index:05d}") for index in range(count)]


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


def plan(*drugs: str) -> QueryPlan:
    return QueryPlan(
        cohorts=[Cohort(label=drug, filters=Filters(drug=drug)) for drug in drugs],
        dimension=Dimension.PHASE,
    )


async def run(query_plan: QueryPlan, **thresholds: Any) -> VisualizationResponse:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        max_records=MAX_RECORDS,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
    )
    async with CtGovClient(settings, cache=NoCache()) as client:
        interpretation = Interpretation(
            question="q", plan=query_plan, planned_by="structured_fields"
        )
        return await pipeline.execute(
            interpretation,
            client=client,
            data_quality_thresholds=DataQualityThresholds(**thresholds),
        )


async def test_truncated_result_is_partial(mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies", name="studies").mock(
        return_value=studies_page(studies("1"), total=16_913)
    )
    response = await run(plan("pembrolizumab"))

    assert response.meta.truncated is True
    assert response.meta.data_quality == DataQuality.PARTIAL
    assert response.visualization.title == (
        "Trials by phase for pembrolizumab (partial: first 3 of 16,913 matches)"
    )
    [note] = [a for a in response.meta.assumptions if SAMPLE_NOTE in a]
    assert "first 3 of 16,913 matches ordered by StudyFirstPostDate desc" in note
    assert TIE_NOTE in note
    for call in mock_api["studies"].calls:
        assert call.request.url.params["sort"] == "StudyFirstPostDate:desc"


async def test_complete_result_is_not_partial(mock_api: respx.MockRouter) -> None:
    complete = httpx.Response(200, json={"studies": studies("1", 2), "totalCount": 2})
    mock_api.get("/studies", name="studies").mock(return_value=complete)
    response = await run(plan("pembrolizumab"))

    assert response.meta.data_quality == DataQuality.NORMAL
    assert response.visualization.title == "Trials by phase for pembrolizumab"
    assert not any(SAMPLE_NOTE in a for a in response.meta.assumptions)


async def test_truncated_and_degraded_keeps_the_degraded_warning(
    mock_api: respx.MockRouter,
) -> None:
    page = studies("1")
    set_path(page[0], "protocolSection.designModule.phases", "PHASE9")
    mock_api.get("/studies", name="studies").mock(return_value=studies_page(page, total=500))
    response = await run(plan("pembrolizumab"), min_skipped_for_degraded=1)

    assert response.meta.data_quality == DataQuality.PARTIAL
    assert response.meta.warnings[0].startswith("DEGRADED DATA:")
    assert response.visualization.title.endswith("(partial: first 3 of 500 matches)")


async def test_only_truncated_cohorts_are_named_in_the_title(mock_api: respx.MockRouter) -> None:
    def by_drug(request: httpx.Request) -> httpx.Response:
        if "nivolumab" in request.url.params["filter.advanced"]:
            return httpx.Response(200, json={"studies": studies("2", 2), "totalCount": 2})
        return studies_page(studies("1"), total=2_575)

    mock_api.get("/studies", name="studies").mock(side_effect=by_drug)
    response = await run(plan("pembrolizumab", "nivolumab"))

    assert response.meta.data_quality == DataQuality.PARTIAL
    assert response.visualization.title.endswith(
        " (partial: pembrolizumab first 3 of 2,575 matches)"
    )
    notes = [a for a in response.meta.assumptions if SAMPLE_NOTE in a]
    assert len(notes) == 1
    assert notes[0].startswith("pembrolizumab: ")
