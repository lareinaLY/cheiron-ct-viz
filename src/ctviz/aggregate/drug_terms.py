"""Which intervention MeSH terms in a cohort contain the queried drug name.

A drug filter matches intervention names or MeSH terms, so one name can pull in several
molecules (conjugates, biosimilars, tracers), and some trials match by name only, with
no MeSH term for the drug. Both facts are computed here from the records; nothing is
inferred about which term the user meant.
"""

from __future__ import annotations

from collections.abc import Sequence

from ctviz.schemas.response import MatchedTerm
from ctviz.schemas.trial import TrialRecord


def matching_terms(record: TrialRecord, drug: str) -> set[str]:
    """The record's intervention MeSH terms that contain `drug` (case-insensitive)."""
    needle = drug.casefold()
    return {mesh.term for mesh in record.intervention_mesh_terms if needle in mesh.term.casefold()}


def matched_terms(records: Sequence[TrialRecord], drug: str) -> list[MatchedTerm]:
    """Distinct matching terms with their trial counts, most trials first (ties by name)."""
    trials: dict[str, int] = {}
    for record in records:
        for term in matching_terms(record, drug):
            trials[term] = trials.get(term, 0) + 1
    return [
        MatchedTerm(term=term, trials=count)
        for term, count in sorted(trials.items(), key=lambda item: (-item[1], item[0]))
    ]


def hub_assumptions(records: Sequence[TrialRecord], drug: str) -> list[str]:
    """State how many cohort trials add no edges to the queried drug's node.

    Such trials matched the drug filter by intervention name only, so every edge weight
    at the hub is a lower bound by that many trials. When no record carries a matching
    term at all (e.g. a brand name whose MeSH term is the generic), the hub cannot be
    identified by name, which is stated instead of guessed.
    """
    without = sum(1 for record in records if not matching_terms(record, drug))
    if without == len(records):
        return [
            f"No intervention MeSH term contains '{drug}', so the queried drug's node "
            "cannot be identified by name; it may appear under its MeSH name."
        ]
    if without == 0:
        return []
    return [
        f"{without} trials match {drug} by name only and have no {drug} MeSH term; "
        "they add no edges to the hub."
    ]
