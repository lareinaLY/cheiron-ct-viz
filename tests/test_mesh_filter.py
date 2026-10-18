"""Drug network nodes are kept by MeSH tree branch, not by an observed deny-list."""

from __future__ import annotations

import pytest

from ctviz.aggregate.mesh_filter import MeshNodeFilter, MeshVerdict
from ctviz.aggregate.registry import DRUG_NODE_FILTER, NETWORK_COMPARATOR_MESH
from ctviz.data import load_mesh_trees

TREES = {
    "Carboplatin": ["D02.257.125"],
    "Radiotherapy": ["E02.815"],
    "Placebos": ["D26.660", "E02.785"],
    "Platinum": ["D01.268.556.690"],
    "Solutions": ["D26.776"],
    "Immune Checkpoint Inhibitors": ["D27.505.519.507", "D27.505.954.248.384.500"],
    "Adjuvants, Pharmaceutic": ["D26.090", "D27.505.696.399.450.050"],
    "Aspirin": ["D02.241.223.100", "D27.505.696.663.850.014"],
    "Immunotherapy, Active": ["E02.095"],
}


def tree_filter(comparators: dict[str, str] | None = None) -> MeshNodeFilter:
    return MeshNodeFilter(trees=TREES, comparators=comparators or {})


class TestRule:
    def test_supplementary_concept_ids_are_kept_without_a_tree_number(self) -> None:
        assert tree_filter().classify("C582435", "pembrolizumab") is MeshVerdict.KEPT

    def test_descriptor_in_chemicals_and_drugs_branch_is_kept(self) -> None:
        assert tree_filter().classify("D016190", "Carboplatin") is MeshVerdict.KEPT

    def test_descriptor_outside_branch_d_is_excluded(self) -> None:
        verdict = tree_filter().classify("D011878", "Radiotherapy")
        assert verdict is MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH
        assert not verdict.kept

    def test_any_d_tree_number_is_enough(self) -> None:
        assert tree_filter().classify("D010919", "Placebos") is MeshVerdict.KEPT

    def test_descriptor_only_in_d26_or_d27_is_a_drug_class(self) -> None:
        for term in ("Solutions", "Immune Checkpoint Inhibitors", "Adjuvants, Pharmaceutic"):
            verdict = tree_filter().classify("D000001", term)
            assert verdict is MeshVerdict.DRUG_CLASS, term
            assert verdict.kept

    def test_a_specific_drug_tree_number_outweighs_a_d27_action(self) -> None:
        assert tree_filter().classify("D001241", "Aspirin") is MeshVerdict.KEPT

    def test_supplementary_concepts_are_never_drug_classes(self) -> None:
        assert tree_filter().classify("C582435", "Solutions") is MeshVerdict.KEPT

    def test_descriptor_missing_from_tree_is_kept_and_flagged(self) -> None:
        verdict = tree_filter().classify("D999999", "Not A Current Descriptor")
        assert verdict is MeshVerdict.UNMATCHED_IN_TREE
        assert verdict.kept

    def test_tree_lookup_is_by_exact_preferred_name(self) -> None:
        assert tree_filter().classify("D016190", "carboplatin") is MeshVerdict.UNMATCHED_IN_TREE

    def test_comparator_list_wins_over_branch_d(self) -> None:
        comparators = {"D010919": "Placebos"}
        verdict = tree_filter(comparators).classify("D010919", "Placebos")
        assert verdict is MeshVerdict.COMPARATOR
        assert not verdict.kept

    def test_comparators_are_matched_by_id(self) -> None:
        comparators = {"D010919": "Placebos"}
        assert tree_filter(comparators).classify("D000001", "Placebos") is MeshVerdict.KEPT

    def test_term_without_mesh_id_is_looked_up_by_name(self) -> None:
        # Records fetched without the InterventionMeshId projection key nodes by term.
        assert tree_filter().classify("Radiotherapy", "Radiotherapy") is (
            MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH
        )
        assert tree_filter().classify("Carboplatin", "Carboplatin") is MeshVerdict.KEPT

    def test_a_name_starting_with_c_is_not_a_supplementary_concept_id(self) -> None:
        assert tree_filter().classify("Chemo", "Chemo") is MeshVerdict.UNMATCHED_IN_TREE


class TestComparatorList:
    def test_only_comparators_in_branch_d_are_listed(self) -> None:
        assert NETWORK_COMPARATOR_MESH == {
            "D000077330": "Saline Solution",
            "D010919": "Placebos",
        }


class TestPackagedTrees:
    def test_loads_preferred_names_with_tree_numbers(self) -> None:
        trees = load_mesh_trees()
        assert len(trees) > 30_000
        assert trees["Carboplatin"] == ["D02.257.125"]


# Each entry of the former observed deny-list, and the anchors named in the task, with the
# verdict the tree rule gives on the packaged MeSH 2025 trees.
@pytest.mark.parametrize(
    ("mesh_id", "term", "verdict"),
    [
        ("D059039", "Standard of Care", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D019370", "Observation", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D057832", "Watchful Waiting", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D000077330", "Saline Solution", MeshVerdict.COMPARATOR),
        ("D013660", "Taxes", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D012847", "Single Person", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D001336", "Automobiles", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D003226", "Congresses as Topic", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        # D26.776 Pharmaceutical Solutions: kept, but as a dosage form, not a specific drug.
        ("D012996", "Solutions", MeshVerdict.DRUG_CLASS),
        ("D000082082", "Immune Checkpoint Inhibitors", MeshVerdict.DRUG_CLASS),
        ("D000970", "Antineoplastic Agents", MeshVerdict.DRUG_CLASS),
        ("D016190", "Carboplatin", MeshVerdict.KEPT),
        ("C582435", "pembrolizumab", MeshVerdict.KEPT),
        ("D010984", "Platinum", MeshVerdict.KEPT),
        ("D011878", "Radiotherapy", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D010919", "Placebos", MeshVerdict.COMPARATOR),
        ("D013048", "Specimen Handling", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        ("D004358", "Drug Therapy", MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH),
        (
            "D009682",
            "Magnetic Resonance Spectroscopy",
            MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH,
        ),
    ],
)
def test_verdict_on_packaged_trees(mesh_id: str, term: str, verdict: MeshVerdict) -> None:
    assert DRUG_NODE_FILTER.classify(mesh_id, term) is verdict
