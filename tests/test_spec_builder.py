"""Golden tests for spec.build_response.

Each case builds an aggregation result from fixture studies and compares the full
VisualizationResponse JSON with a reviewed snapshot in tests/golden/.
Regenerate after an intended change with: UPDATE_GOLDEN=1 uv run pytest tests/test_spec_builder.py
and review the diff before committing it.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from ctviz.aggregate import build_network, group_cohorts
from ctviz.normalize import normalize_study
from ctviz.retrieval.query_builder import search_semantics
from ctviz.schemas.plan import ChartType, Cohort, Dimension, Filters, QueryPlan, Relation
from ctviz.schemas.response import CohortStats, FieldType, VisualizationResponse
from ctviz.schemas.trial import Phase, TrialRecord
from ctviz.spec import build_response
from tests.factories import load_fixture_studies, make_raw_study

GOLDEN_DIR = Path(__file__).parent / "golden"
AS_OF = date(2026, 10, 17)
DATA_TIMESTAMP = datetime(2026, 10, 16, 9, 0, 5)


def fixture_records(*names: str) -> list[TrialRecord]:
    """Normalized records from fixtures, first occurrence of each NCT ID only."""
    records: dict[str, TrialRecord] = {}
    for name in names:
        for raw in load_fixture_studies(name):
            record = normalize_study(raw)
            records.setdefault(record.nct_id, record)
    return list(records.values())


def stats(label: str, count: int) -> CohortStats:
    return CohortStats(
        label=label,
        total_matched=count,
        records_retrieved=count,
        records_skipped=0,
        truncated=False,
        coverage=1.0,
        reconciled=True,
    )


def respond(
    plan: QueryPlan, result: Any, question: str, counts: list[int]
) -> VisualizationResponse:
    return build_response(
        plan,
        result,
        question=question,
        cohort_stats=[stats(c.label, n) for c, n in zip(plan.cohorts, counts, strict=True)],
        search_semantics=search_semantics(plan.cohorts[0].filters, plan.match_mode),
        data_timestamp=DATA_TIMESTAMP,
        timing_ms={},
    )


def bar_phase() -> VisualizationResponse:
    records = fixture_records("studies_pembrolizumab_page")
    plan = QueryPlan(
        cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
        dimension=Dimension.PHASE,
    )
    result = group_cohorts([("pembrolizumab", records)], Dimension.PHASE, as_of=AS_OF)
    return respond(plan, result, "pembrolizumab trials by phase", [len(records)])


def time_series_start_year() -> VisualizationResponse:
    records = fixture_records(
        "studies_pembrolizumab_page", "studies_multi_phase", "studies_multi_country"
    )
    records.append(
        normalize_study(
            make_raw_study("NCT09000001", start_date={"date": "2026-09", "type": "ESTIMATED"})
        )
    )
    records.append(normalize_study(make_raw_study("NCT09000002")))
    filters = Filters(drug="pembrolizumab", start_year_from=2014)
    plan = QueryPlan(
        cohorts=[Cohort(label="pembrolizumab, started 2014 or later", filters=filters)],
        dimension=Dimension.START_YEAR,
    )
    label = plan.cohorts[0].label
    result = group_cohorts([(label, records)], Dimension.START_YEAR, as_of=AS_OF)
    return respond(plan, result, "pembrolizumab trials per start year", [len(records)])


def grouped_bar_two_cohorts() -> VisualizationResponse:
    pembrolizumab = fixture_records("studies_pembrolizumab_page")
    multi_phase = fixture_records("studies_multi_phase")
    plan = QueryPlan(
        cohorts=[
            Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab")),
            Cohort(
                label="pembrolizumab, Phase 1 or Phase 2",
                filters=Filters(drug="pembrolizumab", phases=[Phase.PHASE1, Phase.PHASE2]),
            ),
        ],
        dimension=Dimension.PHASE,
    )
    result = group_cohorts(
        [(plan.cohorts[0].label, pembrolizumab), (plan.cohorts[1].label, multi_phase)],
        Dimension.PHASE,
        as_of=AS_OF,
    )
    return respond(plan, result, "compare phases", [len(pembrolizumab), len(multi_phase)])


def network_sponsor_drug() -> VisualizationResponse:
    records = fixture_records("studies_pembrolizumab_page")
    plan = QueryPlan(
        cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
        relation=Relation.SPONSOR_DRUG,
    )
    graph = build_network(records, Relation.SPONSOR_DRUG)
    return respond(plan, graph, "who sponsors pembrolizumab with which drugs", [len(records)])


CASES = {
    "bar_phase": bar_phase,
    "time_series_start_year": time_series_start_year,
    "grouped_bar_two_cohorts": grouped_bar_two_cohorts,
    "network_sponsor_drug": network_sponsor_drug,
}


@pytest.mark.parametrize("name", list(CASES))
def test_matches_golden_snapshot(name: str) -> None:
    actual = json.loads(CASES[name]().model_dump_json())
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n")
    assert path.exists(), f"missing snapshot {path}; run with UPDATE_GOLDEN=1 and review it"
    assert actual == json.loads(path.read_text())


class TestTitles:
    def test_dimension_title(self) -> None:
        assert bar_phase().visualization.title == "Trials by phase for pembrolizumab"

    def test_comparison_title(self) -> None:
        assert grouped_bar_two_cohorts().visualization.title == (
            "Trials by phase: pembrolizumab vs pembrolizumab, Phase 1 or Phase 2"
        )

    def test_network_title(self) -> None:
        assert network_sponsor_drug().visualization.title == (
            "Lead sponsor-drug network for pembrolizumab"
        )

    def test_top_n_in_title(self) -> None:
        records = fixture_records("studies_pembrolizumab_page")
        plan = QueryPlan(
            cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
            dimension=Dimension.COUNTRY,
            top_n=3,
        )
        result = group_cohorts(
            [("pembrolizumab", records)], Dimension.COUNTRY, top_n=3, as_of=AS_OF
        )
        response = respond(plan, result, "q", [len(records)])
        assert response.visualization.title == (
            "Trials by location country for pembrolizumab (top 3)"
        )
        assert response.meta.top_n == result.top_n


class TestStructure:
    def test_bar_encoding(self) -> None:
        encoding = bar_phase().visualization.encoding
        assert encoding.x is not None
        assert encoding.y is not None
        assert encoding.x.type == FieldType.NOMINAL
        assert encoding.x.sort == ["Phase 1", "Phase 1/Phase 2", "Phase 2", "Phase 3"]
        assert encoding.y.field == "count"
        assert encoding.y.type == FieldType.QUANTITATIVE

    def test_time_series_encoding_is_temporal(self) -> None:
        response = time_series_start_year()
        assert response.visualization.type == ChartType.TIME_SERIES
        assert response.visualization.encoding.x is not None
        assert response.visualization.encoding.x.type == FieldType.TEMPORAL
        assert response.visualization.encoding.x.time_unit == "year"

    def test_time_series_flags(self) -> None:
        data = {d.key: d for d in time_series_start_year().visualization.data}
        assert data["2026"].incomplete is True
        assert data["2026"].estimated_count == 1
        assert data["2027"].estimated_count == 1
        assert data["Unknown"].is_unknown is True
        assert any("time axis" in a for a in time_series_start_year().meta.assumptions)

    def test_grouped_bar_colors_by_cohort(self) -> None:
        response = grouped_bar_two_cohorts()
        assert response.visualization.type == ChartType.GROUPED_BAR_CHART
        assert response.visualization.encoding.color is not None
        assert response.visualization.encoding.color.field == "cohort"
        assert {d.cohort for d in response.visualization.data} == {
            "pembrolizumab",
            "pembrolizumab, Phase 1 or Phase 2",
        }

    def test_grouped_bar_meta_sums_cohorts(self) -> None:
        meta = grouped_bar_two_cohorts().meta
        assert meta.total_matched == 8
        assert meta.records_retrieved == 8
        assert [c.label for c in meta.cohorts] == [
            "pembrolizumab",
            "pembrolizumab, Phase 1 or Phase 2",
        ]

    def test_network_uses_cytoscape_elements(self) -> None:
        response = network_sponsor_drug()
        assert response.visualization.data == []
        node_ids = {n.data.id for n in response.visualization.nodes}
        for edge in response.visualization.edges:
            assert edge.data.id == f"{edge.data.source}--{edge.data.target}"
            assert {edge.data.source, edge.data.target} <= node_ids

    @pytest.mark.parametrize("name", list(CASES))
    def test_every_datum_cited(self, name: str) -> None:
        visualization = CASES[name]().visualization
        cited = [*visualization.data, *(n.data for n in visualization.nodes)]
        cited += [e.data for e in visualization.edges]
        assert cited
        assert all(item.citations for item in cited)

    def test_meta_states_count_unit_and_interpretation(self) -> None:
        meta = bar_phase().meta
        assert meta.count_unit == "trials"
        assert meta.interpretation.question == "pembrolizumab trials by phase"
        assert meta.interpretation.planned_by == "structured_fields"
        assert meta.data_timestamp == DATA_TIMESTAMP
        assert "drug" in meta.search_semantics


class TestDateTypeReporting:
    def test_datum_carries_per_bucket_date_type_counts(self) -> None:
        data = {d.key: d for d in time_series_start_year().visualization.data}
        # NCT02129556 starts "2014-12" with no startDateStruct.type (legacy record).
        assert data["2014"].unknown_date_type_count == 1
        assert data["2014"].estimated_count == 0
        assert data["2026"].estimated_count == 1
        assert data["2026"].unknown_date_type_count == 0

    def test_meta_totals(self) -> None:
        meta = time_series_start_year().meta
        assert meta.estimated_count == 2
        assert meta.unknown_date_type_count == 1
        assert any("no ACTUAL/ESTIMATED type" in w for w in meta.warnings)

    def test_meta_totals_not_applicable_for_networks(self) -> None:
        meta = network_sponsor_drug().meta
        assert meta.unknown_date_type_count is None
        assert meta.estimated_count is None

    def test_meta_totals_sum_cohorts(self) -> None:
        meta = grouped_bar_two_cohorts().meta
        assert meta.estimated_count == 0
        assert meta.unknown_date_type_count is None


class TestTemporalTitle:
    def test_title_uses_granularity(self) -> None:
        response = time_series_start_year()
        assert response.visualization.title == (
            "Trials by start year for pembrolizumab, started 2014 or later"
        )
        assert response.visualization.encoding.x is not None
        assert response.visualization.encoding.x.title == "Start year"

    def test_warning_text_unchanged(self) -> None:
        warnings = time_series_start_year().meta.warnings
        assert "1 of 10 trials have no start date; they are counted in the 'Unknown' bucket." in (
            warnings
        )
        assert "2 trials are counted using an estimated start date." in warnings
