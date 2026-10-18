"""A batch with poison records must still answer: dirty upstream data never fails a request.

20 good fixture-derived records plus five dirty ones: no NCT ID (structural: skipped),
an unknown phase, a free-text start date and an unknown status (value-domain: kept and
bucketed as Unrecognized, or Unknown for dates), and a brand-new top-level section
(ignored).
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.schemas.response import Datum, VisualizationResponse
from tests.factories import SOURCE_PATHS, fixture_study, load_fixture_studies, messages, set_path

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
POISON_BASE = ("studies_pembrolizumab_page", "NCT02318771")  # Phase 1, COMPLETED, 2015, OTHER

PHASE5_ID = "NCT90000101"
SOON_ID = "NCT90000102"
PAUSED_ID = "NCT90000103"
NEW_SECTION_ID = "NCT90000104"


def renumbered(study: dict[str, Any], nct_id: str) -> dict[str, Any]:
    clone = copy.deepcopy(study)
    set_path(clone, SOURCE_PATHS["nct_id"], nct_id)
    return clone


def good_studies() -> list[dict[str, Any]]:
    """20 good records: four renumbered copies of each pembrolizumab fixture study."""
    fixtures = load_fixture_studies("studies_pembrolizumab_page")
    return [
        renumbered(study, f"NCT9000{copy_index}{index:03d}")
        for copy_index in range(4)
        for index, study in enumerate(fixtures)
    ]


def poison_studies() -> list[dict[str, Any]]:
    base = fixture_study(*POISON_BASE)

    no_nct_id = copy.deepcopy(base)
    del no_nct_id["protocolSection"]["identificationModule"]["nctId"]

    phase5 = renumbered(base, PHASE5_ID)
    set_path(phase5, SOURCE_PATHS["phases"], ["PHASE5"])

    soon = renumbered(base, SOON_ID)
    set_path(soon, SOURCE_PATHS["start_date"], "soon")

    paused = renumbered(base, PAUSED_ID)
    set_path(paused, SOURCE_PATHS["overall_status"], "PAUSED_FOREVER")

    new_section = renumbered(base, NEW_SECTION_ID)
    new_section["futureSection"] = {"aiSummaryModule": {"summary": "never seen before"}}

    return [no_nct_id, phase5, soon, paused, new_section]


BATCH = good_studies() + poison_studies()


@pytest.fixture
def api() -> Iterator[TestClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
        cache_enabled=False,
        enum_drift_check=False,
    )
    with (
        respx.mock(base_url=BASE_URL, assert_all_called=False) as router,
        TestClient(create_app(settings)) as client,
    ):
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        router.get("/studies").mock(
            return_value=httpx.Response(200, json={"studies": BATCH, "totalCount": len(BATCH)})
        )
        yield client


def ask(api: TestClient, dimension: str) -> VisualizationResponse:
    response = api.post(
        "/query", json={"query": "poison", "drug": "pembrolizumab", "dimension": dimension}
    )
    assert response.status_code == 200, response.text
    return VisualizationResponse.model_validate(response.json())


def datum(body: VisualizationResponse, key: str) -> Datum:
    (match,) = [d for d in body.visualization.data if d.key == key]
    return match


def cited(item: Datum) -> set[str]:
    return {citation.nct_id for citation in item.citations}


def test_good_batch_has_twenty_records() -> None:
    assert len(good_studies()) == 20
    assert len({s["protocolSection"]["identificationModule"]["nctId"] for s in BATCH[:20]}) == 20


@pytest.mark.parametrize("dimension", ["phase", "start_year", "overall_status", "sponsor_class"])
def test_request_succeeds_and_counts_every_readable_record(api: TestClient, dimension: str) -> None:
    body = ask(api, dimension)
    meta = body.meta
    assert meta.records_retrieved == 25
    assert meta.cohorts[0].records_skipped == 1
    assert meta.cohorts[0].reconciled is True
    assert meta.data_quality == "normal"
    # Single-valued dimensions: 20 good + 4 kept poison records, each counted once.
    assert sum(d.count for d in body.visualization.data) == 24


def test_unknown_phase_is_in_an_unrecognized_bucket(api: TestClient) -> None:
    body = ask(api, "phase")
    unrecognized = datum(body, "Unrecognized")
    assert unrecognized.count == 1
    assert unrecognized.is_unrecognized is True
    assert unrecognized.is_unknown is False
    assert unrecognized.citations[0].nct_id == PHASE5_ID
    assert unrecognized.citations[0].value == ["PHASE5"]
    # 4 good copies of the Phase 1 study, plus the date, status and new-section records.
    phase1 = datum(body, "Phase 1")
    assert phase1.count == 7
    assert {SOON_ID, PAUSED_ID, NEW_SECTION_ID} <= cited(phase1)


def test_unknown_status_is_in_an_unrecognized_bucket(api: TestClient) -> None:
    body = ask(api, "overall_status")
    unrecognized = datum(body, "Unrecognized")
    assert unrecognized.count == 1
    assert unrecognized.citations[0].nct_id == PAUSED_ID
    assert unrecognized.citations[0].value == "PAUSED_FOREVER"
    assert datum(body, "COMPLETED").count == 19
    assert datum(body, "TERMINATED").count == 4


def test_free_text_date_is_unknown_on_time_but_counted_elsewhere(api: TestClient) -> None:
    start_year = ask(api, "start_year")
    unknown = datum(start_year, "Unknown")
    assert unknown.count == 1
    assert unknown.citations[0].nct_id == SOON_ID
    assert unknown.citations[0].value == "soon"
    assert all(not d.is_unrecognized for d in start_year.visualization.data)
    assert datum(start_year, "2015").count == 7

    sponsor_class = ask(api, "sponsor_class")
    assert SOON_ID in cited(datum(sponsor_class, "OTHER"))
    status = ask(api, "overall_status")
    assert SOON_ID in cited(datum(status, "COMPLETED"))


def test_new_section_is_ignored(api: TestClient) -> None:
    body = ask(api, "phase")
    assert NEW_SECTION_ID in cited(datum(body, "Phase 1"))
    assert not any(
        "futureSection" in w or NEW_SECTION_ID in w for w in messages(body.meta.warnings)
    )


def test_warnings_list_each_case(api: TestClient) -> None:
    warnings = messages(ask(api, "phase").meta.warnings)
    skipped = [w for w in warnings if w.startswith("Skipped unknown:")]
    assert len(skipped) == 1
    assert "NCT ID" in skipped[0]
    assert any("1 of 25 retrieved trials were skipped" in w for w in warnings)
    for value, nct_id in (
        ("'PHASE5'", PHASE5_ID),
        ("'soon'", SOON_ID),
        ("'PAUSED_FOREVER'", PAUSED_ID),
    ):
        (warning,) = [w for w in warnings if value in w]
        assert nct_id in warning
        assert "1 trial " in warning
