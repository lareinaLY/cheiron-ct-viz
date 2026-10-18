"""Network rules found on live data: drug node filter, readability caps, size budget."""

from __future__ import annotations

import pytest

from ctviz.aggregate import build_network
from ctviz.config import Settings
from ctviz.schemas.aggregate import Graph
from ctviz.schemas.plan import Cohort, Filters, QueryPlan, Relation
from ctviz.schemas.response import (
    CohortStats,
    ExcludedTerm,
    ExclusionReason,
    NetworkEdge,
    NetworkSummary,
    NodeType,
    UnmatchedTerm,
    VisualizationResponse,
)
from ctviz.schemas.trial import AgencyClass, MeshTerm, Sponsor, TrialRecord
from ctviz.spec import build_response
from tests.factories import make_record, mesh, messages

STANDARD_OF_CARE = mesh("Standard of Care", "D059039")
TAXES = mesh("Taxes", "D013660")
RADIOTHERAPY = mesh("Radiotherapy", "D011878")
PEMBROLIZUMAB = mesh("pembrolizumab", "C582435")
CARBOPLATIN = mesh("Carboplatin", "D016190")
PLATINUM = mesh("Platinum", "D010984")
SALINE = mesh("Saline Solution", "D000077330")
UNMATCHED = mesh("Retired Descriptor", "D999999")
CHECKPOINT_INHIBITORS = mesh("Immune Checkpoint Inhibitors", "D000082082")
SOLUTIONS = mesh("Solutions", "D012996")
OFF_BRANCH = ExclusionReason.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH


def drugs(nct_id: str, *terms: MeshTerm) -> TrialRecord:
    return make_record(nct_id, intervention_mesh_terms=list(terms))


def labels(graph: Graph) -> set[str]:
    return {node.label for node in graph.nodes}


def edge_weights(graph: Graph) -> dict[frozenset[str], int]:
    by_id = {node.id: node.label for node in graph.nodes}
    return {frozenset({by_id[e.source], by_id[e.target]}): e.weight for e in graph.edges}


def summary_of(graph: Graph) -> NetworkSummary:
    assert graph.summary is not None
    return graph.summary


class TestDrugNodeFilter:
    def test_terms_outside_the_drug_branch_and_comparators_are_not_nodes(self) -> None:
        records = [
            drugs("NCT1", PEMBROLIZUMAB, CARBOPLATIN, STANDARD_OF_CARE),
            drugs("NCT2", PEMBROLIZUMAB, TAXES, SALINE),
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert labels(graph) == {"pembrolizumab", "Carboplatin"}
        assert edge_weights(graph) == {frozenset({"pembrolizumab", "Carboplatin"}): 1}

    def test_excluded_terms_listed_with_reason_and_trial_counts(self) -> None:
        records = [
            drugs("NCT1", PEMBROLIZUMAB, STANDARD_OF_CARE),
            drugs("NCT2", PEMBROLIZUMAB, STANDARD_OF_CARE, TAXES, SALINE),
            drugs("NCT3", PEMBROLIZUMAB),
        ]
        summary = summary_of(build_network(records, Relation.DRUG_DRUG_COOCCURRENCE))
        assert summary.excluded_terms == [
            ExcludedTerm(id="D059039", label="Standard of Care", reason=OFF_BRANCH, trials=2),
            ExcludedTerm(
                id="D000077330",
                label="Saline Solution",
                reason=ExclusionReason.COMPARATOR,
                trials=1,
            ),
            ExcludedTerm(id="D013660", label="Taxes", reason=OFF_BRANCH, trials=1),
        ]
        assert summary.trials_with_excluded_terms == 2

    def test_exclusion_stated_in_assumptions(self) -> None:
        records = [drugs("NCT1", PEMBROLIZUMAB, STANDARD_OF_CARE), drugs("NCT2", TAXES)]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        (message,) = [a for a in messages(graph.assumptions) if "not drawn as nodes" in a]
        assert "2 intervention MeSH terms" in message
        assert "Standard of Care (D059039, not_in_chemicals_and_drugs_branch): 1 trial" in message
        assert "2 trials affected" in message
        assert "meta.network.excluded_terms" in message

    def test_no_exclusion_assumption_when_nothing_excluded(self) -> None:
        graph = build_network([drugs("NCT1", PEMBROLIZUMAB)], Relation.DRUG_DRUG_COOCCURRENCE)
        assert not any("not drawn as nodes" in a for a in messages(graph.assumptions))
        assert summary_of(graph).excluded_terms == []

    def test_unmatched_descriptors_are_kept_and_reported(self) -> None:
        records = [
            drugs("NCT1", PEMBROLIZUMAB, UNMATCHED),
            drugs("NCT2", PEMBROLIZUMAB, UNMATCHED),
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert labels(graph) == {"pembrolizumab", "Retired Descriptor"}
        summary = summary_of(graph)
        assert summary.unmatched_terms == [
            UnmatchedTerm(id="D999999", label="Retired Descriptor", trials=2)
        ]
        assert summary.unmatched_terms[0].reason == "unmatched_in_tree"
        assert summary.excluded_terms == []
        (message,) = [a for a in messages(graph.assumptions) if "unmatched_in_tree" in a]
        assert "Retired Descriptor (D999999): 2 trials" in message

    def test_supplementary_concepts_are_never_reported(self) -> None:
        graph = build_network([drugs("NCT1", PEMBROLIZUMAB)], Relation.DRUG_DRUG_COOCCURRENCE)
        assert summary_of(graph).unmatched_terms == []

    def test_comparator_matched_by_mesh_id_not_by_text(self) -> None:
        records = [
            drugs(
                "NCT1",
                PEMBROLIZUMAB,
                mesh("Saline Solution", "D000001"),
                mesh("Renamed saline", "D000077330"),
            )
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert labels(graph) == {"pembrolizumab", "Saline Solution"}

    def test_applies_to_every_relation_with_drug_nodes(self) -> None:
        record = make_record(
            "NCT1",
            lead_sponsor=Sponsor(name="Acme", agency_class=AgencyClass.INDUSTRY),
            conditions=["Melanoma"],
            intervention_mesh_terms=[PEMBROLIZUMAB, STANDARD_OF_CARE, RADIOTHERAPY],
        )
        for relation in Relation:
            graph = build_network([record], relation)
            assert labels(graph) & {"Standard of Care", "Radiotherapy"} == set(), relation

    def test_only_drug_nodes_are_filtered(self) -> None:
        record = make_record(
            "NCT1",
            lead_sponsor=Sponsor(name="Taxes", agency_class=AgencyClass.OTHER),
            conditions=["Radiotherapy"],
            intervention_mesh_terms=[PEMBROLIZUMAB],
        )
        assert "Taxes" in labels(build_network([record], Relation.SPONSOR_DRUG))
        assert "Radiotherapy" in labels(build_network([record], Relation.CONDITION_DRUG))

    def test_platinum_kept_and_radiotherapy_excluded(self) -> None:
        records = [drugs("NCT1", PEMBROLIZUMAB, PLATINUM, RADIOTHERAPY)]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert labels(graph) == {"pembrolizumab", "Platinum"}

    def test_drug_classes_are_kept_with_their_own_node_type(self) -> None:
        records = [drugs("NCT1", PEMBROLIZUMAB, CHECKPOINT_INHIBITORS, CARBOPLATIN)]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        types = {node.label: node.type for node in graph.nodes}
        assert types == {
            "pembrolizumab": NodeType.DRUG,
            "Immune Checkpoint Inhibitors": NodeType.DRUG_CLASS,
            "Carboplatin": NodeType.DRUG,
        }
        assert "drug:D000082082" in {node.id for node in graph.nodes}
        assert len(graph.edges) == 3

    def test_drug_classes_counted_in_summary_and_not_listed_as_excluded(self) -> None:
        records = [
            drugs("NCT1", PEMBROLIZUMAB, CHECKPOINT_INHIBITORS),
            drugs("NCT2", PEMBROLIZUMAB, SOLUTIONS, CHECKPOINT_INHIBITORS),
        ]
        summary = summary_of(build_network(records, Relation.DRUG_DRUG_COOCCURRENCE))
        assert summary.drug_class_nodes_total == 2
        assert summary.drug_class_nodes_shown == 2
        assert summary.excluded_terms == []
        assert summary.unmatched_terms == []

    def test_drug_class_count_follows_the_node_cap(self) -> None:
        records = [drugs(f"NCT{i}", PEMBROLIZUMAB, CARBOPLATIN) for i in range(3)] + [
            drugs("NCTX", PEMBROLIZUMAB, SOLUTIONS)
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=2)
        summary = summary_of(graph)
        assert summary.drug_class_nodes_total == 1
        assert summary.drug_class_nodes_shown == 0

    def test_drug_classes_stated_in_one_assumption(self) -> None:
        records = [drugs("NCT1", PEMBROLIZUMAB, CHECKPOINT_INHIBITORS, SOLUTIONS)]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        (message,) = [a for a in messages(graph.assumptions) if "drug_class" in a]
        assert "2 nodes" in message
        assert "D26" in message and "D27" in message
        assert "Immune Checkpoint Inhibitors" in message

    def test_no_drug_class_assumption_without_drug_classes(self) -> None:
        graph = build_network([drugs("NCT1", PEMBROLIZUMAB)], Relation.DRUG_DRUG_COOCCURRENCE)
        assert not any("drug_class" in a for a in messages(graph.assumptions))
        assert summary_of(graph).drug_class_nodes_total == 0

    def test_node_type_limitation_stated(self) -> None:
        graph = build_network([drugs("NCT1", PEMBROLIZUMAB)], Relation.DRUG_DRUG_COOCCURRENCE)
        assert any(
            "does not link MeSH terms to intervention types" in a
            for a in messages(graph.assumptions)
        )


def hub_records() -> list[TrialRecord]:
    """Degrees with weight >= 2: H=3 (A, B, C), A=2, B=2, C=1."""
    pairs = [("H", "A"), ("H", "B"), ("H", "C"), ("A", "B")]
    return [
        drugs(f"NCT{left}{right}{copy}", mesh(left, f"M{left}"), mesh(right, f"M{right}"))
        for left, right in pairs
        for copy in range(2)
    ]


class TestReadabilityCaps:
    def test_no_caps_by_default(self) -> None:
        records = [*hub_records(), drugs("NCT9", mesh("X", "MX"), mesh("Y", "MY"))]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert labels(graph) == {"H", "A", "B", "C", "X", "Y"}
        assert len(graph.edges) == 5
        summary = summary_of(graph)
        assert (summary.max_nodes, summary.min_edge_weight) == (None, 1)
        assert summary.nodes_shown == summary.nodes_total == 6
        assert not any("trimmed" in w for w in messages(graph.warnings))

    def test_edges_below_min_weight_are_dropped_and_counted(self) -> None:
        records = [*hub_records(), drugs("NCT9", mesh("X", "MX"), mesh("Y", "MY"))]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE, min_edge_weight=2)
        assert frozenset({"X", "Y"}) not in edge_weights(graph)
        summary = summary_of(graph)
        assert summary.edges_total == 5
        assert summary.edges_below_min_weight == 1
        assert summary.edges_shown == 4
        assert summary.trials_without_shown_edge == 1

    def test_keeps_highest_degree_nodes(self) -> None:
        graph = build_network(
            hub_records(), Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=3, min_edge_weight=2
        )
        assert labels(graph) == {"H", "A", "B"}
        assert set(edge_weights(graph)) == {
            frozenset({"H", "A"}),
            frozenset({"H", "B"}),
            frozenset({"A", "B"}),
        }
        summary = summary_of(graph)
        assert (summary.nodes_total, summary.nodes_shown) == (4, 3)
        assert summary.edges_with_omitted_node == 1
        assert summary.trials_with_edges == 8
        assert summary.trials_without_shown_edge == 2
        # Both H+C trials are still visible through node H.
        assert summary.trials_without_shown_node == 0

    def test_ranks_by_weighted_degree_not_edge_count(self) -> None:
        # Z has 3 edges of weight 1 (one umbrella-style trial each); H has 1 edge of weight 10.
        records = [
            drugs("NCTZP", mesh("Z", "MZ"), mesh("P", "MP")),
            drugs("NCTZQ", mesh("Z", "MZ"), mesh("Q", "MQ")),
            drugs("NCTZR", mesh("Z", "MZ"), mesh("R", "MR")),
            *(drugs(f"NCTHA{i}", mesh("H", "MH"), mesh("A", "MA")) for i in range(10)),
        ]
        graph = build_network(
            records, Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=2, min_edge_weight=1
        )
        assert labels(graph) == {"H", "A"}

    def test_degree_is_counted_after_the_weight_threshold(self) -> None:
        records = [
            drugs("NCT1", mesh("H", "MH"), mesh("A", "MA")),
            drugs("NCT2", mesh("H", "MH"), mesh("A", "MA")),
            drugs("NCT3", mesh("Z", "MZ"), mesh("P", "MP"), mesh("Q", "MQ"), mesh("R", "MR")),
        ]
        graph = build_network(
            records, Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=2, min_edge_weight=2
        )
        assert labels(graph) == {"H", "A"}
        summary = summary_of(graph)
        assert summary.trials_without_shown_node == 1

    def test_node_size_is_not_recomputed_after_trimming(self) -> None:
        graph = build_network(
            hub_records(), Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=3, min_edge_weight=2
        )
        sizes = {node.label: node.size for node in graph.nodes}
        assert sizes["H"] == 6

    def test_trimming_reported_in_warning(self) -> None:
        graph = build_network(
            hub_records(), Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=3, min_edge_weight=2
        )
        (message,) = [w for w in messages(graph.warnings) if "trimmed" in w]
        assert "3 of 4 nodes" in message
        assert "3 of 4 edges" in message
        assert "MAX_NETWORK_NODES" in message
        assert "MIN_EDGE_WEIGHT" in message
        assert "2 trials" in message

    def test_sponsor_drug_trimming_keeps_both_node_types_eligible(self) -> None:
        merck = Sponsor(name="Merck", agency_class=AgencyClass.INDUSTRY)
        records = [
            make_record(f"NCT{i}", lead_sponsor=merck, intervention_mesh_terms=[PEMBROLIZUMAB])
            for i in range(3)
        ]
        graph = build_network(records, Relation.SPONSOR_DRUG, max_nodes=50, min_edge_weight=2)
        assert labels(graph) == {"Merck", "pembrolizumab"}
        assert edge_weights(graph) == {frozenset({"Merck", "pembrolizumab"}): 3}


class TestHub:
    def test_queried_drug_stays_as_hub(self) -> None:
        records = [
            drugs(f"NCT{i}", PEMBROLIZUMAB, mesh(name, f"M{name}"))
            for i, name in enumerate(["A", "A", "B", "B", "C", "C"])
        ]
        graph = build_network(
            records, Relation.DRUG_DRUG_COOCCURRENCE, max_nodes=2, min_edge_weight=2
        )
        assert "pembrolizumab" in labels(graph)

    def test_edge_weight_counts_distinct_trials(self) -> None:
        records = [
            # One trial listing the hub twice (alias entries share the MeSH id).
            drugs("NCT1", PEMBROLIZUMAB, mesh("Keytruda", "C582435"), mesh("A", "MA")),
            drugs("NCT2", PEMBROLIZUMAB, mesh("A", "MA"), mesh("A", "MA")),
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        assert edge_weights(graph) == {frozenset({"pembrolizumab", "A"}): 2}
        (edge,) = graph.edges
        assert {c.nct_id for c in edge.citations} == {"NCT1", "NCT2"}


def network_response(
    graph: Graph, *, max_response_bytes: int | None, reduced_edge_citation_cap: int = 2
) -> VisualizationResponse:
    plan = QueryPlan(
        cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
        relation=graph.relation,
    )
    stats = CohortStats(
        label="pembrolizumab",
        total_matched=10,
        records_retrieved=10,
        records_skipped=0,
        truncated=False,
        coverage=1.0,
        reconciled=True,
    )
    return build_response(
        plan,
        graph,
        question="q",
        cohort_stats=[stats],
        search_semantics={},
        data_timestamp=None,
        timing_ms={},
        max_response_bytes=max_response_bytes,
        reduced_edge_citation_cap=reduced_edge_citation_cap,
    )


def busy_graph() -> Graph:
    records = [
        drugs(f"NCT{i:03d}", PEMBROLIZUMAB, mesh("A", "MA"), mesh("B", "MB")) for i in range(10)
    ]
    return build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)


def edges_of(response: VisualizationResponse) -> list[NetworkEdge]:
    return [element.data for element in response.visualization.edges]


class TestResponseSizeBudget:
    def test_edge_citations_reduced_when_over_budget(self) -> None:
        full = network_response(busy_graph(), max_response_bytes=None)
        reduced = network_response(busy_graph(), max_response_bytes=5_000)
        assert [(e.source, e.target, e.weight) for e in edges_of(reduced)] == [
            (e.source, e.target, e.weight) for e in edges_of(full)
        ]
        for before, after in zip(edges_of(full), edges_of(reduced), strict=True):
            assert len(after.citations) == 2
            assert after.citations == before.citations[:2]
            assert after.citation_count_total == before.citation_count_total
        summary = reduced.meta.network
        assert summary is not None
        assert summary.edge_citations_reduced is True
        assert summary.edge_citation_cap == 2
        assert summary.response_bytes_before_reduction == len(full.model_dump_json().encode())
        assert any("citations per edge" in w for w in messages(reduced.meta.warnings))

    def test_no_reduction_under_budget(self) -> None:
        response = network_response(busy_graph(), max_response_bytes=10_000_000)
        summary = response.meta.network
        assert summary is not None
        assert summary.edge_citations_reduced is False
        assert summary.response_bytes_before_reduction is None
        assert all(len(e.citations) == 20 for e in edges_of(response))

    def test_network_summary_reaches_meta(self) -> None:
        graph = busy_graph()
        assert network_response(graph, max_response_bytes=None).meta.network == graph.summary


class TestSettings:
    def test_defaults(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.max_network_nodes == 50
        assert settings.min_edge_weight == 2
        assert settings.network_max_response_bytes == 1_000_000

    def test_conventional_env_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_NETWORK_NODES", "30")
        monkeypatch.setenv("MIN_EDGE_WEIGHT", "3")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert (settings.max_network_nodes, settings.min_edge_weight) == (30, 3)
