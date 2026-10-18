"""Decide which intervention MeSH terms are drug network nodes, by MeSH tree branch.

Study-level intervention MeSH is derived by automated text mapping, so it holds
procedures, diagnostics and outright mis-mappings next to drugs ("Specimen Handling",
"Drug Therapy", "Taxes" from "taxane"). A drug network keeps a term only when MeSH itself
files it under Chemicals and Drugs:
- a supplementary concept record (id "C…"): these are overwhelmingly chemicals and drugs,
  and they have no tree numbers at all (pembrolizumab is C582435), so they are kept as is;
- a descriptor (id "D…", or a term without an id) with any tree number in branch D.
  When all its tree numbers are in D26 (Pharmaceutical Preparations) or D27 (Chemical
  Actions and Uses) it is a drug class or dosage form ("Immune Checkpoint Inhibitors",
  "Solutions"), not a specific drug: it is kept and marked as such.
Descriptors in branch D that are comparators without active treatment are dropped by an
explicit, id-matched list. A descriptor whose name is not in the packaged trees is kept and
flagged rather than dropped on missing evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

SUPPLEMENTARY_CONCEPT_ID = re.compile(r"C\d+")
CHEMICALS_AND_DRUGS_BRANCH = "D"
DRUG_CLASS_SUBTREES = frozenset({"D26", "D27"})


class MeshVerdict(StrEnum):
    """Outcome of classifying one intervention MeSH term as a drug node."""

    KEPT = "kept"
    DRUG_CLASS = "drug_class"
    UNMATCHED_IN_TREE = "unmatched_in_tree"
    COMPARATOR = "comparator"
    NOT_IN_CHEMICALS_AND_DRUGS_BRANCH = "not_in_chemicals_and_drugs_branch"

    @property
    def kept(self) -> bool:
        """True when the term becomes a node (possibly with a report, if unmatched)."""
        return self in (MeshVerdict.KEPT, MeshVerdict.DRUG_CLASS, MeshVerdict.UNMATCHED_IN_TREE)


@dataclass(frozen=True)
class MeshNodeFilter:
    """Tree-branch rule for drug nodes.

    `trees` maps a descriptor's preferred name to its tree numbers; `comparators` maps the
    MeSH ids of branch-D comparators to their terms.
    """

    trees: Mapping[str, Sequence[str]] = field(repr=False)
    comparators: Mapping[str, str]

    def classify(self, key: str, term: str) -> MeshVerdict:
        """Classify a term by its node key (the MeSH id, or the term when there is none).

        Comparators are matched by id because terms can be renamed; the tree lookup is by
        exact preferred name because that is how the NLM tree file is keyed.
        """
        if SUPPLEMENTARY_CONCEPT_ID.fullmatch(key):
            return MeshVerdict.KEPT
        if key in self.comparators:
            return MeshVerdict.COMPARATOR
        tree_numbers = self.trees.get(term)
        if tree_numbers is None:
            return MeshVerdict.UNMATCHED_IN_TREE
        if not any(number.startswith(CHEMICALS_AND_DRUGS_BRANCH) for number in tree_numbers):
            return MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH
        if all(number.split(".")[0] in DRUG_CLASS_SUBTREES for number in tree_numbers):
            return MeshVerdict.DRUG_CLASS
        return MeshVerdict.KEPT
