"""Tests for aggregate.group_by and aggregate.build_network."""

from __future__ import annotations

from datetime import date

from ctviz.aggregate import UNKNOWN_BUCKET_KEY, build_network, group_by
from ctviz.schemas.aggregate import Bucket, Graph, GroupResult
from ctviz.schemas.citation import CitationRole
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import NetworkEdge, NodeType
from ctviz.schemas.trial import (
    AgencyClass,
    Intervention,
    OverallStatus,
    Phase,
    Sponsor,
    TrialRecord,
)
from tests.factories import make_record, mesh

AS_OF = date(2026, 10, 17)


def bucket(result: GroupResult, key: str) -> Bucket:
    matches = [b for b in result.buckets if b.key == key]
    assert len(matches) == 1, f"expected exactly one bucket {key!r}, got {len(matches)}"
    return matches[0]


def edges_by_labels(graph: Graph) -> dict[frozenset[str], NetworkEdge]:
    labels = {node.id: node.label for node in graph.nodes}
    return {frozenset({labels[e.source], labels[e.target]}): e for e in graph.edges}


def phase_records() -> list[TrialRecord]:
    return [
        make_record("NCT1", phases=[Phase.PHASE1, Phase.PHASE2], phase_bucket="Phase 1/Phase 2"),
        make_record("NCT2", phases=[Phase.PHASE2], phase_bucket="Phase 2"),
        make_record("NCT3", phases=[Phase.PHASE2], phase_bucket="Phase 2"),
        make_record("NCT4", phases=[Phase.NA], phase_bucket="Not Applicable"),
        make_record("NCT5", phases=[], phase_bucket="Unknown"),
    ]


class TestPhase:
    def test_buckets_are_mutually_exclusive(self) -> None:
        records = phase_records()
        result = group_by(records, Dimension.PHASE, as_of=AS_OF)
        assert sum(b.count for b in result.buckets) == len(records)
        assert result.buckets_overlap is False
        assert bucket(result, "Phase 1/Phase 2").count == 1
        assert bucket(result, "Phase 2").count == 2

    def test_multi_phase_trial_not_counted_in_single_phase_buckets(self) -> None:
        result = group_by(phase_records(), Dimension.PHASE, as_of=AS_OF)
        phase_2_ids = {c.nct_id for c in bucket(result, "Phase 2").citations}
        assert phase_2_ids == {"NCT2", "NCT3"}
        assert all(b.key != "Phase 1" for b in result.buckets)

    def test_unknown_and_not_applicable_never_merged(self) -> None:
        result = group_by(phase_records(), Dimension.PHASE, as_of=AS_OF)
        unknown = bucket(result, UNKNOWN_BUCKET_KEY)
        not_applicable = bucket(result, "Not Applicable")
        assert unknown.count == 1
        assert unknown.is_unknown is True
        assert not_applicable.count == 1
        assert not_applicable.is_unknown is False

    def test_citation_cites_exact_source_value(self) -> None:
        result = group_by(phase_records(), Dimension.PHASE, as_of=AS_OF)
        (citation,) = bucket(result, "Phase 1/Phase 2").citations
        assert citation.nct_id == "NCT1"
        assert citation.field_path == "protocolSection.designModule.phases"
        assert citation.value == ["PHASE1", "PHASE2"]
        assert citation.url == "https://clinicaltrials.gov/study/NCT1"
        assert citation.role == CitationRole.PRIMARY

    def test_unknown_bucket_cites_absent_value(self) -> None:
        result = group_by(phase_records(), Dimension.PHASE, as_of=AS_OF)
        (citation,) = bucket(result, UNKNOWN_BUCKET_KEY).citations
        assert citation.nct_id == "NCT5"
        assert citation.field_path == "protocolSection.designModule.phases"
        assert citation.value is None


class TestCitationCap:
    def test_citations_capped_but_total_is_true_count(self) -> None:
        records = [make_record(f"NCT{i}", phase_bucket="Phase 2") for i in range(5)]
        result = group_by(records, Dimension.PHASE, citation_cap=2, as_of=AS_OF)
        phase_2 = bucket(result, "Phase 2")
        assert phase_2.count == 5
        assert len(phase_2.citations) == 2
        assert phase_2.citation_count_total == 5
        assert {c.nct_id for c in phase_2.citations} <= {r.nct_id for r in records}

    def test_no_cap_hit_keeps_all_citations(self) -> None:
        records = [make_record(f"NCT{i}", phase_bucket="Phase 3") for i in range(3)]
        result = group_by(records, Dimension.PHASE, citation_cap=10, as_of=AS_OF)
        assert len(bucket(result, "Phase 3").citations) == 3
        assert bucket(result, "Phase 3").citation_count_total == 3


class TestCountry:
    def records(self) -> list[TrialRecord]:
        return [
            make_record("NCT1", countries=["United States", "France"]),
            make_record("NCT2", countries=["United States"]),
            make_record("NCT3", countries=[]),
        ]

    def test_multi_country_trial_counted_in_each_country(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        assert bucket(result, "United States").count == 2
        assert bucket(result, "France").count == 1
        assert {c.nct_id for c in bucket(result, "France").citations} == {"NCT1"}

    def test_overlap_flagged(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        assert result.buckets_overlap is True

    def test_single_country_trial_counted_once(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        nct2_appearances = sum(1 for b in result.buckets for c in b.citations if c.nct_id == "NCT2")
        assert nct2_appearances == 1

    def test_trial_without_locations_is_unknown(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        unknown = bucket(result, UNKNOWN_BUCKET_KEY)
        assert unknown.count == 1
        assert unknown.is_unknown is True

    def test_country_citation(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        (citation,) = bucket(result, "France").citations
        assert citation.field_path == "protocolSection.contactsLocationsModule.locations[].country"
        assert citation.value == "France"


class TestStartYear:
    def records(self) -> list[TrialRecord]:
        return [
            make_record(
                "NCT1",
                start_date=date(2024, 5, 1),
                start_date_raw="2024-05",
                start_date_precision="month",
                start_date_type="actual",
            ),
            make_record(
                "NCT2",
                start_date=date(2024, 8, 9),
                start_date_raw="2024-08-09",
                start_date_precision="day",
                start_date_type="estimated",
            ),
            make_record(
                "NCT3",
                start_date=date(2026, 1, 2),
                start_date_raw="2026-01-02",
                start_date_precision="day",
                start_date_type="actual",
            ),
            make_record(
                "NCT4",
                start_date=date(2026, 12, 1),
                start_date_raw="2026-12",
                start_date_precision="month",
                start_date_type="estimated",
            ),
            make_record("NCT5"),
        ]

    def test_estimated_dates_included_and_counted(self) -> None:
        result = group_by(self.records(), Dimension.START_YEAR, as_of=AS_OF)
        year_2024 = bucket(result, "2024")
        assert year_2024.count == 2
        assert year_2024.estimated_count == 1
        assert bucket(result, "2026").estimated_count == 1

    def test_current_year_bucket_flagged_incomplete(self) -> None:
        result = group_by(self.records(), Dimension.START_YEAR, as_of=AS_OF)
        assert bucket(result, "2026").incomplete is True
        assert bucket(result, "2024").incomplete is False

    def test_chronological_with_unknown_last(self) -> None:
        result = group_by(self.records(), Dimension.START_YEAR, as_of=AS_OF)
        known = [b.key for b in result.buckets if not b.is_unknown]
        assert known == sorted(known)
        assert result.buckets[-1].key == UNKNOWN_BUCKET_KEY
        assert result.buckets[-1].count == 1

    def test_citation_value_is_raw_date_string(self) -> None:
        result = group_by(self.records(), Dimension.START_YEAR, as_of=AS_OF)
        citations = {c.nct_id: c for c in bucket(result, "2024").citations}
        assert citations["NCT1"].field_path == "protocolSection.statusModule.startDateStruct.date"
        assert citations["NCT1"].value == "2024-05"

    def test_not_overlapping(self) -> None:
        result = group_by(self.records(), Dimension.START_YEAR, as_of=AS_OF)
        assert result.buckets_overlap is False
        assert sum(b.count for b in result.buckets) == 5


class TestStartMonth:
    def test_month_buckets_and_current_month_incomplete(self) -> None:
        records = [
            make_record("NCT1", start_date=date(2026, 9, 1), start_date_raw="2026-09"),
            make_record("NCT2", start_date=date(2026, 10, 3), start_date_raw="2026-10-03"),
        ]
        result = group_by(records, Dimension.START_MONTH, as_of=AS_OF)
        assert bucket(result, "2026-09").incomplete is False
        assert bucket(result, "2026-10").incomplete is True


class TestApiUnknownVersusMissing:
    def test_overall_status_unknown_is_not_missing(self) -> None:
        records = [
            make_record("NCT1", overall_status=OverallStatus.UNKNOWN),
            make_record("NCT2", overall_status=None),
        ]
        result = group_by(records, Dimension.OVERALL_STATUS, as_of=AS_OF)
        api_unknown = bucket(result, OverallStatus.UNKNOWN.value)
        missing = bucket(result, UNKNOWN_BUCKET_KEY)
        assert api_unknown.count == 1
        assert api_unknown.is_unknown is False
        assert missing.count == 1
        assert missing.is_unknown is True

    def test_sponsor_class_unknown_is_not_missing(self) -> None:
        records = [
            make_record("NCT1", lead_sponsor=Sponsor(name="X", agency_class=AgencyClass.UNKNOWN)),
            make_record("NCT2", lead_sponsor=None),
        ]
        result = group_by(records, Dimension.SPONSOR_CLASS, as_of=AS_OF)
        assert bucket(result, AgencyClass.UNKNOWN.value).is_unknown is False
        assert bucket(result, UNKNOWN_BUCKET_KEY).is_unknown is True


class TestInterventionMesh:
    def records(self) -> list[TrialRecord]:
        return [
            make_record(
                "NCT1",
                interventions=[
                    Intervention(name_raw="pembrolizumab (KEYTRUDA®)", type_raw="DRUG"),
                    Intervention(name_raw="carboplatin", type_raw="DRUG"),
                ],
                intervention_mesh_terms=[
                    mesh("pembrolizumab", "C582435"),
                    mesh("Carboplatin", "D016190"),
                ],
            ),
            make_record(
                "NCT2",
                interventions=[Intervention(name_raw="MK-3475", type_raw="BIOLOGICAL")],
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            ),
            make_record(
                "NCT3",
                interventions=[Intervention(name_raw="SEA-CD40", type_raw="DRUG")],
                intervention_mesh_terms=[],
            ),
        ]

    def test_grouped_by_mesh_id_and_labelled_by_term(self) -> None:
        result = group_by(self.records(), Dimension.INTERVENTION_MESH, as_of=AS_OF)
        pembrolizumab = bucket(result, "C582435")
        assert pembrolizumab.label == "pembrolizumab"
        assert pembrolizumab.count == 2
        assert bucket(result, "D016190").count == 1
        assert result.buckets_overlap is True

    def test_trial_without_mesh_is_unknown(self) -> None:
        result = group_by(self.records(), Dimension.INTERVENTION_MESH, as_of=AS_OF)
        unknown = bucket(result, UNKNOWN_BUCKET_KEY)
        assert unknown.count == 1
        assert unknown.is_unknown is True

    def test_primary_citation_is_study_level_mesh_term(self) -> None:
        result = group_by(self.records(), Dimension.INTERVENTION_MESH, as_of=AS_OF)
        primary = [c for c in bucket(result, "D016190").citations if c.role == CitationRole.PRIMARY]
        assert len(primary) == 1
        assert primary[0].nct_id == "NCT1"
        assert primary[0].field_path == "derivedSection.interventionBrowseModule.meshes[1].term"
        assert primary[0].value == "Carboplatin"

    def test_raw_intervention_names_attached_as_supporting(self) -> None:
        result = group_by(self.records(), Dimension.INTERVENTION_MESH, as_of=AS_OF)
        supporting = {
            (c.field_path, c.value)
            for c in bucket(result, "D016190").citations
            if c.role == CitationRole.SUPPORTING and c.nct_id == "NCT1"
        }
        # All raw names of the study, with no inferred link to the MeSH term.
        assert supporting == {
            (
                "protocolSection.armsInterventionsModule.interventions[0].name",
                "pembrolizumab (KEYTRUDA®)",
            ),
            ("protocolSection.armsInterventionsModule.interventions[1].name", "carboplatin"),
        }

    def test_duplicate_mesh_in_one_study_counted_once(self) -> None:
        records = [
            make_record(
                "NCT1",
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")] * 2,
            )
        ]
        result = group_by(records, Dimension.INTERVENTION_MESH, as_of=AS_OF)
        assert bucket(result, "C582435").count == 1


class TestSponsorDrugNetwork:
    def records(self) -> list[TrialRecord]:
        merck = Sponsor(name="Merck Sharp & Dohme LLC", agency_class=AgencyClass.INDUSTRY)
        seagen = Sponsor(name="Seagen Inc.", agency_class=AgencyClass.INDUSTRY)
        return [
            make_record(
                "NCT1",
                lead_sponsor=merck,
                intervention_mesh_terms=[
                    mesh("pembrolizumab", "C582435"),
                    mesh("olaparib", "C531550"),
                ],
            ),
            make_record(
                "NCT2",
                lead_sponsor=merck,
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            ),
            make_record(
                "NCT3",
                lead_sponsor=seagen,
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            ),
        ]

    def test_edge_weight_is_cooccurring_trial_count(self) -> None:
        graph = build_network(self.records(), Relation.SPONSOR_DRUG)
        edges = edges_by_labels(graph)
        assert edges[frozenset({"Merck Sharp & Dohme LLC", "pembrolizumab"})].weight == 2
        assert edges[frozenset({"Merck Sharp & Dohme LLC", "olaparib"})].weight == 1
        assert edges[frozenset({"Seagen Inc.", "pembrolizumab"})].weight == 1
        assert len(edges) == 3

    def test_node_types_labelled(self) -> None:
        graph = build_network(self.records(), Relation.SPONSOR_DRUG)
        types = {node.label: node.type for node in graph.nodes}
        assert types == {
            "Merck Sharp & Dohme LLC": NodeType.SPONSOR,
            "Seagen Inc.": NodeType.SPONSOR,
            "pembrolizumab": NodeType.DRUG,
            "olaparib": NodeType.DRUG,
        }

    def test_node_size_is_trial_count(self) -> None:
        graph = build_network(self.records(), Relation.SPONSOR_DRUG)
        sizes = {node.label: node.size for node in graph.nodes}
        assert sizes["pembrolizumab"] == 3
        assert sizes["Merck Sharp & Dohme LLC"] == 2

    def test_edge_citations_list_contributing_trials(self) -> None:
        graph = build_network(self.records(), Relation.SPONSOR_DRUG)
        edge = edges_by_labels(graph)[frozenset({"Merck Sharp & Dohme LLC", "pembrolizumab"})]
        assert {c.nct_id for c in edge.citations} == {"NCT1", "NCT2"}


class TestDrugDrugCooccurrence:
    def test_three_drugs_yield_three_pairwise_edges(self) -> None:
        records = [
            make_record(
                "NCT1",
                intervention_mesh_terms=[mesh("A", "M1"), mesh("B", "M2"), mesh("C", "M3")],
            )
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        edges = edges_by_labels(graph)
        assert set(edges) == {frozenset({"A", "B"}), frozenset({"A", "C"}), frozenset({"B", "C"})}
        assert all(e.weight == 1 for e in edges.values())

    def test_single_drug_trial_yields_no_edge(self) -> None:
        records = [make_record("NCT1", intervention_mesh_terms=[mesh("A", "M1")])]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert graph.edges == []

    def test_weights_accumulate_across_trials(self) -> None:
        records = [
            make_record("NCT1", intervention_mesh_terms=[mesh("A", "M1"), mesh("B", "M2")]),
            make_record("NCT2", intervention_mesh_terms=[mesh("B", "M2"), mesh("A", "M1")]),
            make_record("NCT3", intervention_mesh_terms=[mesh("A", "M1")]),
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        edges = edges_by_labels(graph)
        assert len(edges) == 1
        edge = edges[frozenset({"A", "B"})]
        assert edge.weight == 2
        assert {c.nct_id for c in edge.citations} == {"NCT1", "NCT2"}
