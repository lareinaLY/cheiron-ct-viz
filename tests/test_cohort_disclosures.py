"""Per-cohort disclosures: which MeSH terms a drug name matched, and withdrawn studies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from ctviz import pipeline
from ctviz.aggregate.drug_terms import matched_terms
from ctviz.config import Settings
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import Interpretation, MatchedTerm, VisualizationResponse
from tests.factories import make_raw_study, make_record, mesh, messages, set_path

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
TRASTUZUMAB = {"id": "D000068878", "term": "Trastuzumab"}
T_DXD = {"id": "C000614491", "term": "trastuzumab deruxtecan"}
PACLITAXEL = {"id": "D017239", "term": "Paclitaxel"}
STATUS_PATH = "protocolSection.statusModule.overallStatus"


def study(nct_id: str, *meshes: dict[str, str], status: str = "COMPLETED") -> dict[str, Any]:
    raw = make_raw_study(nct_id, phases=["PHASE2"], intervention_meshes=list(meshes))
    set_path(raw, STATUS_PATH, status)
    return raw


# Two distinct intervention MeSH terms contain "trastuzumab": the antibody and an ADC.
TRASTUZUMAB_STUDIES = [
    study("NCT00000001", TRASTUZUMAB, PACLITAXEL),
    study("NCT00000002", TRASTUZUMAB),
    study("NCT00000003", T_DXD, status="WITHDRAWN"),
]


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


def serve(router: respx.MockRouter, *cohorts: list[dict[str, Any]]) -> None:
    router.get("/studies").mock(
        side_effect=[
            httpx.Response(200, json={"studies": studies, "totalCount": len(studies)})
            for studies in cohorts
        ]
    )


def plan(*filters: Filters) -> QueryPlan:
    return QueryPlan(
        cohorts=[Cohort(label=f.drug or f.condition or "all", filters=f) for f in filters],
        dimension=Dimension.PHASE,
    )


async def run(query_plan: QueryPlan) -> VisualizationResponse:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
    )
    async with CtGovClient(settings, cache=NoCache()) as client:
        interpretation = Interpretation(
            question="q", plan=query_plan, planned_by="structured_fields"
        )
        return await pipeline.execute(interpretation, client=client)


class TestMatchedTerms:
    def test_terms_containing_the_name_are_counted_case_insensitively(self) -> None:
        records = [
            make_record("NCT1", intervention_mesh_terms=[mesh("Trastuzumab"), mesh("Paclitaxel")]),
            make_record("NCT2", intervention_mesh_terms=[mesh("Trastuzumab")]),
            make_record("NCT3", intervention_mesh_terms=[mesh("trastuzumab deruxtecan")]),
        ]
        assert matched_terms(records, "TRASTUZUMAB") == [
            MatchedTerm(term="Trastuzumab", trials=2),
            MatchedTerm(term="trastuzumab deruxtecan", trials=1),
        ]

    async def test_several_terms_are_listed_and_warned(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, TRASTUZUMAB_STUDIES)
        meta = (await run(plan(Filters(drug="trastuzumab")))).meta
        assert meta.cohorts[0].drug_matched_terms == [
            MatchedTerm(term="Trastuzumab", trials=2),
            MatchedTerm(term="trastuzumab deruxtecan", trials=1),
        ]
        assert (
            "'trastuzumab' matched 2 distinct intervention terms: Trastuzumab (2), "
            "trastuzumab deruxtecan (1) — these are different molecules (conjugates, "
            "biosimilars, tracers); narrow the name if you mean one."
        ) in messages(meta.warnings)

    async def test_one_term_is_listed_without_a_warning(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, TRASTUZUMAB_STUDIES[:2])
        meta = (await run(plan(Filters(drug="trastuzumab")))).meta
        assert meta.cohorts[0].drug_matched_terms == [MatchedTerm(term="Trastuzumab", trials=2)]
        assert not any("distinct intervention terms" in w for w in messages(meta.warnings))

    async def test_no_drug_filter_means_no_list(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, TRASTUZUMAB_STUDIES)
        meta = (await run(plan(Filters(condition="breast cancer")))).meta
        assert meta.cohorts[0].drug_matched_terms is None

    async def test_each_cohort_gets_its_own_list(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, TRASTUZUMAB_STUDIES, [study("NCT00000009", PACLITAXEL)])
        meta = (await run(plan(Filters(drug="trastuzumab"), Filters(drug="paclitaxel")))).meta
        assert [len(c.drug_matched_terms or []) for c in meta.cohorts] == [2, 1]
        assert any(
            w.startswith("trastuzumab: 'trastuzumab' matched 2") for w in messages(meta.warnings)
        )


class TestWithdrawn:
    async def test_withdrawn_studies_are_counted_and_warned(
        self, mock_api: respx.MockRouter
    ) -> None:
        serve(mock_api, TRASTUZUMAB_STUDIES)
        meta = (await run(plan(Filters(drug="trastuzumab")))).meta
        assert meta.cohorts[0].withdrawn == 1
        assert (
            "1 withdrawn studies (never enrolled) are included in these counts; add a status "
            "filter to exclude them."
        ) in messages(meta.warnings)

    async def test_no_withdrawn_no_warning(self, mock_api: respx.MockRouter) -> None:
        serve(mock_api, TRASTUZUMAB_STUDIES[:2])
        meta = (await run(plan(Filters(drug="trastuzumab")))).meta
        assert meta.cohorts[0].withdrawn == 0
        assert not any("withdrawn" in w for w in messages(meta.warnings))
