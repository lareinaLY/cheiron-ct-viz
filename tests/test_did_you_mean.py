"""A tiny drug cohort whose records carry a different MeSH term gets one count and a hint."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from ctviz import pipeline
from ctviz.config import Settings
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.request import cohort_label
from ctviz.schemas.response import Interpretation, NoticeCode, NoticeSeverity, VisualizationResponse
from tests.factories import make_raw_study

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
PEMBROLIZUMAB = {"id": "C582435", "term": "pembrolizumab"}


def studies(count: int, *meshes: dict[str, str]) -> list[dict[str, Any]]:
    return [
        make_raw_study(f"NCT{i:08d}", phases=["PHASE2"], intervention_meshes=list(meshes))
        for i in range(count)
    ]


def page(items: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"studies": items, "totalCount": len(items) if total is None else total}
    )


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


async def run(filters: Filters) -> VisualizationResponse:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
    )
    plan = QueryPlan(
        cohorts=[Cohort(label=cohort_label(filters), filters=filters)], dimension=Dimension.PHASE
    )
    async with CtGovClient(settings, cache=NoCache()) as client:
        interpretation = Interpretation(question="q", plan=plan, planned_by="structured_fields")
        return await pipeline.execute(interpretation, client=client)


def did_you_mean(response: VisualizationResponse) -> list[str]:
    return [w.message for w in response.meta.warnings if w.code == NoticeCode.DID_YOU_MEAN]


async def test_pembro_suggests_the_mesh_term_with_its_count(mock_api: respx.MockRouter) -> None:
    route = mock_api.get("/studies").mock(
        side_effect=[page(studies(2, PEMBROLIZUMAB)), page(studies(1, PEMBROLIZUMAB), 2642)]
    )
    response = await run(Filters(drug="pembro"))
    assert did_you_mean(response) == [
        "'pembro' matched 2 studies; the matched records carry the intervention MeSH term "
        "pembrolizumab (2,642 studies) — did you mean that?"
    ]
    (warning,) = [w for w in response.meta.warnings if w.code == NoticeCode.DID_YOU_MEAN]
    assert warning.severity == NoticeSeverity.WARNING
    assert route.call_count == 2
    count_params = route.calls[1].request.url.params
    assert 'AREA[InterventionMeshTerm]"pembrolizumab"' in count_params["filter.advanced"]
    assert count_params["countTotal"] == "true"
    assert sum(d.count for d in response.visualization.data) == 2


async def test_other_filters_are_kept_and_said(mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies").mock(
        side_effect=[page(studies(2, PEMBROLIZUMAB)), page(studies(1, PEMBROLIZUMAB), 300)]
    )
    response = await run(Filters(drug="pembro", condition="melanoma"))
    (message,) = did_you_mean(response)
    assert "pembrolizumab (300 studies with the same other filters)" in message


async def test_no_extra_request_at_twenty_matches(mock_api: respx.MockRouter) -> None:
    route = mock_api.get("/studies").mock(side_effect=[page(studies(20, PEMBROLIZUMAB))])
    response = await run(Filters(drug="pembro"))
    assert did_you_mean(response) == []
    assert route.call_count == 1


async def test_no_hint_when_the_term_is_the_query(mock_api: respx.MockRouter) -> None:
    route = mock_api.get("/studies").mock(side_effect=[page(studies(2, PEMBROLIZUMAB))])
    response = await run(Filters(drug="Pembrolizumab"))
    assert did_you_mean(response) == []
    assert route.call_count == 1


async def test_failed_count_still_gives_the_hint(mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies").mock(
        side_effect=[page(studies(2, PEMBROLIZUMAB)), httpx.Response(500), httpx.Response(500)]
    )
    response = await run(Filters(drug="pembro"))
    (message,) = did_you_mean(response)
    assert message.startswith(
        "'pembro' matched 2 studies; the matched records carry the intervention MeSH term "
        "pembrolizumab — did you mean that? (Its study count could not be retrieved:"
    )


async def test_no_hint_when_the_name_is_itself_a_matched_term(
    mock_api: respx.MockRouter,
) -> None:
    trastuzumab = {"id": "D000068878", "term": "Trastuzumab"}
    t_dxd = {"id": "C000614491", "term": "trastuzumab deruxtecan"}
    adc = make_raw_study("NCT09000001", phases=["PHASE2"], intervention_meshes=[t_dxd])
    route = mock_api.get("/studies").mock(side_effect=[page([*studies(2, trastuzumab), adc])])
    response = await run(Filters(drug="trastuzumab"))
    assert did_you_mean(response) == []
    assert route.call_count == 1
