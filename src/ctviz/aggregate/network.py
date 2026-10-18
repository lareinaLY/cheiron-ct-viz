"""Build co-occurrence networks from TrialRecords."""

from __future__ import annotations

from collections.abc import Sequence

from ctviz.aggregate.group import DEFAULT_CITATION_CAP
from ctviz.schemas.aggregate import Graph
from ctviz.schemas.plan import Relation
from ctviz.schemas.trial import TrialRecord


def build_network(
    records: Sequence[TrialRecord],
    relation: Relation,
    *,
    citation_cap: int = DEFAULT_CITATION_CAP,
) -> Graph:
    """Build an undirected graph for `relation`.

    Drugs are the study-level intervention MeSH terms (keyed by MeSH id when present).
    Sponsors are lead sponsors; conditions are the raw condition strings.
    - Node `size`: number of trials the entity appears in.
    - Edge `weight`: number of trials in which both endpoints co-occur; each trial
      counts once per pair, however many times it lists either entity.
    - Edge citations list the contributing trials.
    """
    raise NotImplementedError("build_network is not implemented yet")
