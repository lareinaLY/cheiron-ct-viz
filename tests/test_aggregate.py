"""Tests for aggregate.group_by and aggregate.build_network."""

from __future__ import annotations

from datetime import date

import pytest

from ctviz.aggregate import UNKNOWN_BUCKET_KEY, build_network, group_by
from ctviz.schemas.aggregate import Bucket, Graph, GroupResult
from ctviz.schemas.citation import CitationRole
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import NetworkEdge, NodeType
from ctviz.schemas.trial import (
    AgencyClass,
    EnrollmentType,
    Intervention,
    OverallStatus,
    Phase,
    Sponsor,
    TrialRecord,
)
from tests.factories import country, make_record, mesh

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
            make_record(
                "NCT1",
                country_locations=[
                    country("United States", location_index=0),
                    country("France", location_index=3),
                ],
            ),
            make_record("NCT2", country_locations=[country("United States", location_index=0)]),
            make_record("NCT3", country_locations=[]),
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

    def test_country_citation_indexes_first_location(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        (citation,) = bucket(result, "France").citations
        assert citation.field_path == "protocolSection.contactsLocationsModule.locations[3].country"
        assert citation.value == "France"

    def test_bucket_key_trimmed_but_citation_value_raw(self) -> None:
        raw = "Bonaire, Saint Eustatius and Saba "
        records = [
            make_record(
                "NCT1",
                country_locations=[country(raw.strip(), value_raw=raw, location_index=2)],
            )
        ]
        result = group_by(records, Dimension.COUNTRY, as_of=AS_OF)
        (citation,) = bucket(result, "Bonaire, Saint Eustatius and Saba").citations
        assert citation.value == raw
        assert citation.field_path == "protocolSection.contactsLocationsModule.locations[2].country"

    def test_unknown_reported_in_warnings(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        assert any("1 of 3" in warning for warning in result.warnings)

    def test_overlap_explained_in_assumptions(self) -> None:
        result = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        assert result.assumptions


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

    def test_year_only_date_is_unknown_for_months_and_cites_raw_value(self) -> None:
        records = [
            make_record(
                "NCT1",
                start_date=date(2026, 1, 1),
                start_date_raw="2026",
                start_date_precision="year",
            ),
            make_record("NCT2", start_date=date(2026, 1, 5), start_date_raw="2026-01-05"),
        ]
        result = group_by(records, Dimension.START_MONTH, as_of=AS_OF)
        assert bucket(result, "2026-01").count == 1
        unknown = bucket(result, UNKNOWN_BUCKET_KEY)
        assert unknown.count == 1
        (citation,) = unknown.citations
        assert citation.value == "2026"
        assert result.warnings

    def test_year_only_date_counts_for_years(self) -> None:
        records = [
            make_record(
                "NCT1",
                start_date=date(2026, 1, 1),
                start_date_raw="2026",
                start_date_precision="year",
            )
        ]
        result = group_by(records, Dimension.START_YEAR, as_of=AS_OF)
        assert bucket(result, "2026").count == 1


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

    def test_trials_without_mesh_reported_with_count(self) -> None:
        result = group_by(self.records(), Dimension.INTERVENTION_MESH, as_of=AS_OF)
        assert any("1 of 3" in warning for warning in result.warnings)

    def test_non_drug_mesh_terms_noted(self) -> None:
        result = group_by(self.records(), Dimension.INTERVENTION_MESH, as_of=AS_OF)
        assert any("non-drug" in assumption for assumption in result.assumptions)

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

    def test_lead_sponsor_only_and_non_drug_nodes_noted(self) -> None:
        graph = build_network(self.records(), Relation.SPONSOR_DRUG)
        assert any("lead sponsor" in assumption for assumption in graph.assumptions)
        assert any("non-drug" in assumption for assumption in graph.assumptions)

    def test_trial_without_sponsor_or_drug_reported(self) -> None:
        records = [*self.records(), make_record("NCT4", intervention_mesh_terms=[])]
        graph = build_network(records, Relation.SPONSOR_DRUG)
        assert any("1 of 4" in warning for warning in graph.warnings)
        assert {node.size for node in graph.nodes if node.label == "pembrolizumab"} == {3}


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


class TestConditionDrugNetwork:
    def test_raw_conditions_linked_to_drugs(self) -> None:
        records = [
            make_record(
                "NCT1",
                conditions=["Melanoma", "Melanoma"],
                intervention_mesh_terms=[mesh("pembrolizumab", "C582435")],
            )
        ]
        graph = build_network(records, Relation.CONDITION_DRUG)
        types = {node.label: node.type for node in graph.nodes}
        assert types == {"Melanoma": NodeType.CONDITION, "pembrolizumab": NodeType.DRUG}
        (edge,) = graph.edges
        assert edge.weight == 1


class TestNetworkCitations:
    def test_edge_cites_both_endpoints_with_exact_values(self) -> None:
        records = [
            make_record(
                "NCT1",
                lead_sponsor=Sponsor(name="Acme", agency_class=AgencyClass.INDUSTRY),
                intervention_mesh_terms=[mesh("A", "M1")],
            )
        ]
        graph = build_network(records, Relation.SPONSOR_DRUG)
        (edge,) = graph.edges
        cited = {(c.field_path, c.value) for c in edge.citations}
        assert cited == {
            ("protocolSection.sponsorCollaboratorsModule.leadSponsor.name", "Acme"),
            ("derivedSection.interventionBrowseModule.meshes[0].term", "A"),
        }
        assert edge.citation_count_total == 2

    def test_edge_citations_capped(self) -> None:
        records = [
            make_record(f"NCT{i}", intervention_mesh_terms=[mesh("A", "M1"), mesh("B", "M2")])
            for i in range(4)
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE, citation_cap=3)
        (edge,) = graph.edges
        assert edge.weight == 4
        assert len(edge.citations) == 3
        assert edge.citation_count_total == 8


class TestOtherDimensions:
    def test_lead_sponsor_cites_name(self) -> None:
        records = [make_record("NCT1", lead_sponsor=Sponsor(name="Acme", agency_class=None))]
        result = group_by(records, Dimension.LEAD_SPONSOR, as_of=AS_OF)
        (citation,) = bucket(result, "Acme").citations
        assert citation.field_path == "protocolSection.sponsorCollaboratorsModule.leadSponsor.name"
        assert result.buckets_overlap is False

    def test_sponsor_class_cites_class(self) -> None:
        records = [
            make_record("NCT1", lead_sponsor=Sponsor(name="A", agency_class=AgencyClass.NIH)),
            make_record("NCT2", lead_sponsor=Sponsor(name="B", agency_class=None)),
        ]
        result = group_by(records, Dimension.SPONSOR_CLASS, as_of=AS_OF)
        (citation,) = bucket(result, "NIH").citations
        assert citation.field_path == "protocolSection.sponsorCollaboratorsModule.leadSponsor.class"
        assert citation.value == "NIH"
        assert bucket(result, UNKNOWN_BUCKET_KEY).count == 1

    def test_intervention_type_counts_each_type_once_per_trial(self) -> None:
        records = [
            make_record(
                "NCT1",
                interventions=[
                    Intervention(name_raw="a", type_raw="DRUG"),
                    Intervention(name_raw="b", type_raw="DRUG"),
                    Intervention(name_raw="c", type_raw="DEVICE"),
                ],
            ),
            make_record("NCT2", interventions=[Intervention(name_raw="d", type_raw="BIOLOGICAL")]),
        ]
        result = group_by(records, Dimension.INTERVENTION_TYPE, as_of=AS_OF)
        assert bucket(result, "DRUG").count == 1
        assert bucket(result, "DEVICE").count == 1
        assert result.buckets_overlap is True
        (citation,) = bucket(result, "DEVICE").citations
        assert (
            citation.field_path == "protocolSection.armsInterventionsModule.interventions[2].type"
        )
        assert citation.value == "DEVICE"

    def test_condition_uses_raw_strings_with_index(self) -> None:
        records = [make_record("NCT1", conditions=["Melanoma", "Carcinoma, Non-Small- Cell Lung"])]
        result = group_by(records, Dimension.CONDITION, as_of=AS_OF)
        (citation,) = bucket(result, "Carcinoma, Non-Small- Cell Lung").citations
        assert citation.field_path == "protocolSection.conditionsModule.conditions[1]"
        assert result.buckets_overlap is True

    def test_categorical_buckets_sorted_by_count_with_unknown_last(self) -> None:
        records = [
            make_record("NCT1", overall_status=OverallStatus.COMPLETED),
            make_record("NCT2", overall_status=OverallStatus.RECRUITING),
            make_record("NCT3", overall_status=OverallStatus.RECRUITING),
            make_record("NCT4", overall_status=None),
        ]
        result = group_by(records, Dimension.OVERALL_STATUS, as_of=AS_OF)
        assert [b.key for b in result.buckets] == ["RECRUITING", "COMPLETED", UNKNOWN_BUCKET_KEY]

    def test_phase_buckets_in_canonical_order(self) -> None:
        result = group_by(phase_records(), Dimension.PHASE, as_of=AS_OF)
        assert [b.key for b in result.buckets] == [
            "Phase 1/Phase 2",
            "Phase 2",
            "Not Applicable",
            UNKNOWN_BUCKET_KEY,
        ]


class TestEnrollmentBucket:
    @pytest.mark.parametrize(
        ("enrollment", "key"),
        [
            (0, "0"),
            (1, "1-49"),
            (49, "1-49"),
            (50, "50-99"),
            (100, "100-499"),
            (500, "500-999"),
            (1000, "1000+"),
        ],
    )
    def test_bin_edges(self, enrollment: int, key: str) -> None:
        records = [make_record("NCT1", enrollment=enrollment)]
        result = group_by(records, Dimension.ENROLLMENT_BUCKET, as_of=AS_OF)
        assert [b.key for b in result.buckets] == [key]

    def test_bins_in_numeric_order_and_missing_is_unknown(self) -> None:
        records = [
            make_record("NCT1", enrollment=793),
            make_record("NCT2", enrollment=22),
            make_record("NCT3", enrollment=None),
            make_record("NCT4", enrollment=1200),
        ]
        result = group_by(records, Dimension.ENROLLMENT_BUCKET, as_of=AS_OF)
        assert [b.key for b in result.buckets] == ["1-49", "500-999", "1000+", UNKNOWN_BUCKET_KEY]

    def test_estimated_enrollment_counted(self) -> None:
        records = [make_record("NCT1", enrollment=22, enrollment_type=EnrollmentType.ESTIMATED)]
        result = group_by(records, Dimension.ENROLLMENT_BUCKET, as_of=AS_OF)
        (only,) = result.buckets
        assert only.estimated_count == 1
        (citation,) = only.citations
        assert citation.field_path == "protocolSection.designModule.enrollmentInfo.count"
        assert citation.value == 22


def test_every_dimension_is_registered() -> None:
    for dimension in Dimension:
        result = group_by([make_record("NCT1")], dimension, as_of=AS_OF)
        assert sum(b.count for b in result.buckets) >= 1


def test_every_relation_is_registered() -> None:
    for relation in Relation:
        assert isinstance(build_network([make_record("NCT1")], relation), Graph)
