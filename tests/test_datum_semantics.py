"""Datum flags, estimate kinds, citation totals, the temporal sort and 'Studies' wording."""

from __future__ import annotations

from datetime import date

import pytest

from ctviz.aggregate import build_network, group_by, group_cohorts
from ctviz.aggregate.registry import DIMENSIONS, EstimateKind
from ctviz.schemas.citation import CitationRole
from ctviz.schemas.plan import ChartType, Cohort, Dimension, Filters, QueryPlan, Relation
from ctviz.schemas.response import CohortStats, VisualizationResponse
from ctviz.schemas.trial import (
    PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY,
    UNKNOWN_BUCKET_KEY,
    AgencyClass,
    EnrollmentType,
    Intervention,
    Phase,
    Sponsor,
    TrialRecord,
)
from ctviz.spec import build_response
from tests.factories import make_record, mesh

AS_OF = date(2026, 10, 17)


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
    dimension: Dimension,
    cohorts: list[tuple[str, list[TrialRecord]]],
    **plan_fields: object,
) -> VisualizationResponse:
    plan = QueryPlan(
        cohorts=[Cohort(label=label, filters=Filters(drug=label)) for label, _ in cohorts],
        dimension=dimension,
        **plan_fields,  # type: ignore[arg-type]
    )
    result = group_cohorts(
        cohorts,
        dimension,
        top_n=plan.top_n,
        bottom_n=plan.bottom_n,
        as_of=AS_OF,
    )
    return build_response(
        plan,
        result,
        question="q",
        cohort_stats=[stats(label, len(records)) for label, records in cohorts],
        search_semantics={},
        data_timestamp=None,
        timing_ms={},
    )


def started(nct_id: str, raw: str, *, estimated: bool = False) -> TrialRecord:
    parts = [int(part) for part in raw.split("-")]
    return make_record(
        nct_id,
        start_date=date(parts[0], parts[1] if len(parts) > 1 else 1, 1),
        start_date_raw=raw,
        start_date_precision="month" if len(parts) > 1 else "year",
        start_date_type="estimated" if estimated else "actual",
    )


class TestNotApplicableFlag:
    def test_both_not_applicable_buckets_are_flagged(self) -> None:
        records = [
            make_record("NCT1", phases=[Phase.NA], phase_bucket="Not Applicable"),
            make_record("NCT2", phase_bucket=PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY),
            make_record("NCT3", phases=[Phase.PHASE2], phase_bucket="Phase 2"),
            make_record("NCT4"),
        ]
        data = respond(Dimension.PHASE, [("a", records)]).visualization.data
        flagged = {datum.key: datum.is_not_applicable for datum in data}
        assert flagged == {
            "Phase 2": False,
            "Not Applicable": True,
            PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY: True,
            UNKNOWN_BUCKET_KEY: False,
        }
        assert not any(datum.is_other for datum in data)


class TestEstimateKind:
    def test_registry_declares_the_kind(self) -> None:
        kinds = {dimension: spec.estimate_kind for dimension, spec in DIMENSIONS.items()}
        assert kinds[Dimension.START_YEAR] == EstimateKind.DATE
        assert kinds[Dimension.START_MONTH] == EstimateKind.DATE
        assert kinds[Dimension.ENROLLMENT_BUCKET] == EstimateKind.ENROLLMENT
        others = {d for d, kind in kinds.items() if kind is None}
        assert others == set(Dimension) - {
            Dimension.START_YEAR,
            Dimension.START_MONTH,
            Dimension.ENROLLMENT_BUCKET,
        }

    def test_phase_has_no_estimate_counts(self) -> None:
        records = [make_record("NCT1", phases=[Phase.PHASE2], phase_bucket="Phase 2")]
        result = group_by(records, Dimension.PHASE, as_of=AS_OF)
        assert result.estimated_count is None
        assert result.unknown_date_type_count is None
        assert result.buckets[0].estimated_count is None
        assert result.buckets[0].unknown_date_type_count is None
        response = respond(Dimension.PHASE, [("a", records)])
        assert response.meta.estimated_count is None
        assert response.visualization.data[0].estimated_count is None

    def test_enrollment_counts_estimates_but_has_no_date_type(self) -> None:
        records = [
            make_record("NCT1", enrollment=40, enrollment_type=EnrollmentType.ESTIMATED),
            make_record("NCT2", enrollment=45, enrollment_type=EnrollmentType.ACTUAL),
        ]
        result = group_by(records, Dimension.ENROLLMENT_BUCKET, as_of=AS_OF)
        assert result.estimated_count == 1
        assert result.unknown_date_type_count is None
        assert result.buckets[0].unknown_date_type_count is None

    def test_start_year_keeps_both_counts(self) -> None:
        result = group_by(
            [started("NCT1", "2020", estimated=True)], Dimension.START_YEAR, as_of=AS_OF
        )
        assert result.estimated_count == 1
        assert result.unknown_date_type_count == 0


class TestCitedTrials:
    def mesh_records(self, count: int) -> list[TrialRecord]:
        return [
            make_record(
                f"NCT{i:03d}",
                interventions=[
                    Intervention(name_raw="Pembrolizumab", type_raw="DRUG"),
                    Intervention(name_raw="Chemo", type_raw="DRUG"),
                ],
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            )
            for i in range(count)
        ]

    def test_datum_counts_distinct_trials(self) -> None:
        (bucket,) = group_by(self.mesh_records(5), Dimension.INTERVENTION_MESH).buckets
        assert bucket.count == 5
        assert bucket.cited_trials_total == 5
        assert bucket.citation_count_total == 15

    def test_cap_applies_to_primary_citations_and_keeps_their_supporting_ones(self) -> None:
        (bucket,) = group_by(
            self.mesh_records(25), Dimension.INTERVENTION_MESH, citation_cap=20
        ).buckets
        primary = [c for c in bucket.citations if c.role == CitationRole.PRIMARY]
        supporting = [c for c in bucket.citations if c.role == CitationRole.SUPPORTING]
        assert len(primary) == 20
        assert len(supporting) == 40
        assert {c.nct_id for c in supporting} == {c.nct_id for c in primary}
        assert bucket.cited_trials_total == 25

    def test_datum_carries_cited_trials_total(self) -> None:
        data = respond(Dimension.INTERVENTION_MESH, [("a", self.mesh_records(3))]).visualization
        assert data.data[0].cited_trials_total == 3

    def test_node_and_edge_count_distinct_trials(self) -> None:
        merck = Sponsor(name="Merck", agency_class=AgencyClass.INDUSTRY)
        records = [
            make_record(
                f"NCT{i}",
                lead_sponsor=merck,
                interventions=[Intervention(name_raw="Keytruda", type_raw="DRUG")],
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            )
            for i in range(3)
        ]
        graph = build_network(records, Relation.SPONSOR_DRUG)
        drug = next(node for node in graph.nodes if node.label == "pembrolizumab")
        assert drug.cited_trials_total == drug.size == 3
        assert drug.citation_count_total == 6
        (edge,) = graph.edges
        assert edge.cited_trials_total == edge.weight == 3
        assert edge.citation_count_total == 6


class TestTemporalSort:
    def test_years_listed_without_gaps_then_unknown(self) -> None:
        records = [started("NCT1", "2015"), started("NCT2", "2018"), make_record("NCT3")]
        x = respond(Dimension.START_YEAR, [("a", records)]).visualization.encoding.x
        assert x is not None
        assert x.sort == ["2015", "2016", "2017", "2018", UNKNOWN_BUCKET_KEY]

    def test_months_cross_the_year_boundary(self) -> None:
        records = [started("NCT1", "2025-11"), started("NCT2", "2026-02")]
        x = respond(Dimension.START_MONTH, [("a", records)]).visualization.encoding.x
        assert x is not None
        assert x.sort == ["2025-11", "2025-12", "2026-01", "2026-02"]

    def test_range_spans_every_cohort(self) -> None:
        response = respond(
            Dimension.START_YEAR,
            [("a", [started("NCT1", "2019")]), ("b", [started("NCT2", "2021")])],
        )
        x = response.visualization.encoding.x
        assert x is not None
        assert x.sort == ["2019", "2020", "2021"]

    def test_bar_chart_of_years_uses_the_same_sort(self) -> None:
        records = [started("NCT1", "2015"), started("NCT2", "2017")]
        response = respond(Dimension.START_YEAR, [("a", records)], chart_hint=ChartType.BAR_CHART)
        x = response.visualization.encoding.x
        assert x is not None
        assert x.sort == ["2015", "2016", "2017"]

    def test_no_gap_filling_when_buckets_were_omitted(self) -> None:
        records = [started("NCT1", "2015"), started("NCT2", "2015"), started("NCT3", "2018")]
        records.append(started("NCT4", "2018"))
        records.append(started("NCT5", "2016"))
        response = respond(Dimension.START_YEAR, [("a", records)], top_n=2)
        x = response.visualization.encoding.x
        assert x is not None
        assert x.sort == ["2015", "2018"]


class TestStudiesWording:
    def test_title_and_axis_say_studies(self) -> None:
        response = respond(Dimension.PHASE, [("pembrolizumab", [make_record("NCT1")])])
        assert response.visualization.title == "Studies by phase for pembrolizumab"
        y = response.visualization.encoding.y
        assert y is not None
        assert y.title == "Studies"

    def test_network_size_channel_says_studies(self) -> None:
        plan = QueryPlan(
            cohorts=[Cohort(label="a", filters=Filters(drug="a"))],
            relation=Relation.SPONSOR_DRUG,
        )
        graph = build_network([make_record("NCT1")], Relation.SPONSOR_DRUG)
        response = build_response(
            plan,
            graph,
            question="q",
            cohort_stats=[stats("a", 1)],
            search_semantics={},
            data_timestamp=None,
            timing_ms={},
        )
        size = response.visualization.encoding.size
        assert size is not None
        assert size.title == "Studies"

    @pytest.mark.parametrize(("count", "text"), [(1, "1 trial has"), (2, "2 trials have")])
    def test_untyped_date_warning_agrees_in_number(self, count: int, text: str) -> None:
        records = [
            make_record(
                f"NCT{i}",
                start_date=date(2020, 1, 1),
                start_date_raw="2020-01",
                start_date_precision="month",
                start_date_type="unknown",
            )
            for i in range(count)
        ]
        result = group_by(records, Dimension.START_YEAR, as_of=AS_OF)
        assert any(w.message.startswith(text) for w in result.warnings)


class TestGroupedSizeBudget:
    def response(self, max_response_bytes: int | None) -> VisualizationResponse:
        records = [
            make_record(f"NCT{i:03d}", phases=[Phase.PHASE2], phase_bucket="Phase 2")
            for i in range(40)
        ]
        plan = QueryPlan(
            cohorts=[Cohort(label="a", filters=Filters(drug="a"))], dimension=Dimension.PHASE
        )
        return build_response(
            plan,
            group_cohorts([("a", records)], Dimension.PHASE, as_of=AS_OF),
            question="q",
            cohort_stats=[stats("a", 40)],
            search_semantics={},
            data_timestamp=None,
            timing_ms={},
            max_response_bytes=max_response_bytes,
            reduced_edge_citation_cap=2,
        )

    def test_citations_cut_but_data_kept_when_over_budget(self) -> None:
        full = self.response(None)
        reduced = self.response(3_000)
        (before,) = full.visualization.data
        (after,) = reduced.visualization.data
        assert after.count == before.count == 40
        assert after.citations == before.citations[:2]
        assert after.cited_trials_total == 40
        (warning,) = [w for w in reduced.meta.warnings if w.code == "citations_reduced"]
        assert "citations per bar were cut to 2" in warning.message

    def test_no_reduction_under_budget(self) -> None:
        response = self.response(10_000_000)
        assert len(response.visualization.data[0].citations) == 20
        assert not [w for w in response.meta.warnings if w.code == "citations_reduced"]


class TestNetworkNodeBudget:
    def response(self, max_response_bytes: int | None) -> VisualizationResponse:
        merck = Sponsor(name="Merck", agency_class=AgencyClass.INDUSTRY)
        names = [Intervention(name_raw=f"Drug number {i}", type_raw="DRUG") for i in range(30)]
        records = [
            make_record(
                f"NCT{i:03d}",
                lead_sponsor=merck,
                interventions=names,
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            )
            for i in range(5)
        ]
        plan = QueryPlan(
            cohorts=[Cohort(label="a", filters=Filters(drug="a"))], relation=Relation.SPONSOR_DRUG
        )
        return build_response(
            plan,
            build_network(records, Relation.SPONSOR_DRUG),
            question="q",
            cohort_stats=[stats("a", 5)],
            search_semantics={},
            data_timestamp=None,
            timing_ms={},
            max_response_bytes=max_response_bytes,
            reduced_edge_citation_cap=2,
        )

    def test_node_supporting_citations_dropped_when_still_over_budget(self) -> None:
        full = self.response(None)
        reduced = self.response(8_000)
        drug = next(n.data for n in reduced.visualization.nodes if n.data.label == "pembrolizumab")
        before = next(n.data for n in full.visualization.nodes if n.data.label == "pembrolizumab")
        assert [c for c in drug.citations if c.role == CitationRole.SUPPORTING] == []
        assert [c for c in drug.citations if c.role == CitationRole.PRIMARY] == [
            c for c in before.citations if c.role == CitationRole.PRIMARY
        ]
        assert drug.cited_trials_total == 5
        summary = reduced.meta.network
        assert summary is not None
        assert summary.node_supporting_citations_dropped is True
        (warning,) = [w for w in reduced.meta.warnings if w.code == "citations_reduced"]
        assert "supporting citations of nodes were dropped" in warning.message

    def test_nodes_untouched_when_edge_cut_suffices(self) -> None:
        response = self.response(10_000_000)
        summary = response.meta.network
        assert summary is not None
        assert summary.node_supporting_citations_dropped is False
