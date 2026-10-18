"""Readability cap: a categorical chart with too many values gets an automatic, stated top N."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz.aggregate import UNKNOWN_BUCKET_KEY, group_cohorts
from ctviz.aggregate.registry import DIMENSIONS
from ctviz.api.app import create_app
from ctviz.config import DEFAULT_MAX_BARS, Settings
from ctviz.schemas.plan import Dimension
from ctviz.schemas.response import NoticeCode, NoticeSeverity, VisualizationResponse
from ctviz.schemas.trial import AgencyClass, Sponsor, TrialRecord
from ctviz.spec.builder import grouped_visualization
from tests.factories import country, make_plan, make_raw_study, make_record, set_path

AS_OF = date(2026, 10, 17)
BASE_URL = "https://ctgov.test/api/v2"


def sponsored(nct_id: str, name: str | None) -> TrialRecord:
    if name is None:
        return make_record(nct_id)
    return make_record(nct_id, lead_sponsor=Sponsor(name=name, agency_class=AgencyClass.OTHER))


def sponsor_records(value_count: int) -> list[TrialRecord]:
    """Sponsor S0 in value_count trials, S1 in one fewer, ..., each value at least once."""
    return [
        sponsored(f"NCT{i:03d}{j:03d}", f"S{i:03d}")
        for i in range(value_count)
        for j in range(max(1, value_count - i))
    ]


def cap_notices(result: Any) -> list[Any]:
    return [w for w in result.warnings if w.code == NoticeCode.READABILITY_CAP]


class TestReadabilityCap:
    def test_default_is_fifty_bars(self) -> None:
        assert DEFAULT_MAX_BARS == 50
        assert Settings(_env_file=None).max_bars == 50  # type: ignore[call-arg]

    def test_too_many_values_get_top_n_with_other_bar_and_a_warning(self) -> None:
        records = [*sponsor_records(6), sponsored("NCTX", None)]
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, max_bars=4, as_of=AS_OF)

        keys = [b.key for b in result.cohorts[0].result.buckets]
        assert keys == ["S000", "S001", "S002", "S003", "Other", UNKNOWN_BUCKET_KEY]
        other = result.cohorts[0].result.buckets[4]
        assert other.is_other and other.count == 2 + 1
        assert result.top_n is not None
        assert result.top_n.top_n == 4
        assert result.top_n.omitted_keys == ["S004", "S005"]
        (notice,) = cap_notices(result)
        assert notice.severity == NoticeSeverity.WARNING
        assert notice.message == (
            "Showing the 4 most frequent of 6 lead sponsors; set top_n for more or fewer."
        )

    def test_message_uses_thousands_separators(self) -> None:
        records = [sponsored(f"NCT{i:05d}", f"S{i:05d}") for i in range(1200)]
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, max_bars=50, as_of=AS_OF)
        (notice,) = cap_notices(result)
        assert notice.message == (
            "Showing the 50 most frequent of 1,200 lead sponsors; set top_n for more or fewer."
        )

    def test_at_the_cap_nothing_changes(self) -> None:
        result = group_cohorts(
            [("a", sponsor_records(4))], Dimension.LEAD_SPONSOR, max_bars=4, as_of=AS_OF
        )
        assert result.top_n is None
        assert len(result.cohorts[0].result.buckets) == 4
        assert cap_notices(result) == []

    def test_unknown_does_not_count_toward_the_cap(self) -> None:
        records = [*sponsor_records(4), sponsored("NCTX", None)]
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, max_bars=4, as_of=AS_OF)
        assert result.top_n is None
        assert cap_notices(result) == []

    @pytest.mark.parametrize("ranking", [{"top_n": 5}, {"bottom_n": 5}])
    def test_an_explicit_ranking_is_never_overridden(self, ranking: dict[str, int]) -> None:
        result = group_cohorts(
            [("a", sponsor_records(8))], Dimension.LEAD_SPONSOR, max_bars=3, as_of=AS_OF, **ranking
        )
        assert result.top_n is not None
        assert result.top_n.top_n == 5
        assert cap_notices(result) == []

    def test_temporal_dimensions_are_not_capped(self) -> None:
        records = [
            make_record(
                f"NCT{m:03d}",
                start_date=date(2020, m, 1),
                start_date_raw=f"2020-{m:02d}",
                start_date_precision="month",
                start_date_type="actual",
            )
            for m in range(1, 13)
        ]
        result = group_cohorts([("a", records)], Dimension.START_MONTH, max_bars=3, as_of=AS_OF)
        assert result.top_n is None
        assert cap_notices(result) == []

    def test_overlapping_dimension_is_capped_without_other_bar(self) -> None:
        records = [
            make_record(
                f"NCT{i:03d}",
                country_locations=[country(f"Country {i}", location_index=0)],
            )
            for i in range(5)
        ]
        result = group_cohorts([("a", records)], Dimension.COUNTRY, max_bars=2, as_of=AS_OF)
        assert result.top_n is not None and result.top_n.top_n == 2
        assert not any(b.is_other for b in result.cohorts[0].result.buckets)
        (notice,) = cap_notices(result)
        assert notice.message == (
            "Showing the 2 most frequent of 5 location countries; set top_n for more or fewer."
        )

    def test_without_max_bars_nothing_is_capped(self) -> None:
        result = group_cohorts([("a", sponsor_records(60))], Dimension.LEAD_SPONSOR, as_of=AS_OF)
        assert result.top_n is None
        assert len(result.cohorts[0].result.buckets) == 60


class TestPluralLabels:
    def test_every_dimension_has_a_readable_plural(self) -> None:
        plurals = {d: spec.plural_label for d, spec in DIMENSIONS.items()}
        assert plurals[Dimension.LEAD_SPONSOR] == "lead sponsors"
        assert plurals[Dimension.COUNTRY] == "location countries"
        assert plurals[Dimension.CONDITION] == "conditions"
        assert plurals[Dimension.INTERVENTION_MESH] == "intervention MeSH terms"


class TestCappedTitle:
    def test_title_states_the_automatic_top_n(self) -> None:
        plan = make_plan(dimension=Dimension.LEAD_SPONSOR)
        result = group_cohorts(
            [(plan.cohorts[0].label, sponsor_records(6))],
            Dimension.LEAD_SPONSOR,
            max_bars=4,
            as_of=AS_OF,
        )
        title = grouped_visualization(plan, result).title
        assert title == "Studies by lead sponsor for cohort 0 (top 4)"

    def test_uncapped_title_has_no_suffix(self) -> None:
        plan = make_plan(dimension=Dimension.LEAD_SPONSOR)
        result = group_cohorts(
            [(plan.cohorts[0].label, sponsor_records(3))],
            Dimension.LEAD_SPONSOR,
            max_bars=4,
            as_of=AS_OF,
        )
        assert grouped_visualization(plan, result).title == "Studies by lead sponsor for cohort 0"


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(
            return_value=httpx.Response(200, json={"dataTimestamp": "2026-10-16T09:00:05"})
        )
        yield router


def test_api_applies_the_configured_cap(mock_api: respx.MockRouter) -> None:
    studies = []
    for i in range(5):
        for j in range(5 - i):
            study = make_raw_study(f"NCT0{i}{j:06d}")
            set_path(study, "protocolSection.sponsorCollaboratorsModule.leadSponsor.name", f"S{i}")
            studies.append(study)
    mock_api.get("/studies").mock(
        return_value=httpx.Response(200, json={"studies": studies, "totalCount": len(studies)})
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        min_request_interval_seconds=0,
        cache_enabled=False,
        enum_drift_check=False,
        max_bars=3,
    )
    with TestClient(create_app(settings)) as api:
        response = api.post(
            "/query", json={"query": "sponsors", "drug": "x", "dimension": "lead_sponsor"}
        )
    assert response.status_code == 200, response.text
    body = VisualizationResponse.model_validate(response.json())
    assert [d.label for d in body.visualization.data] == ["S0", "S1", "S2", "Other (2 values)"]
    assert body.meta.top_n is not None and body.meta.top_n.top_n == 3
    codes = [w.code for w in body.meta.warnings]
    assert NoticeCode.READABILITY_CAP in codes
