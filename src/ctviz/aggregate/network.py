"""Build co-occurrence networks from TrialRecords."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations, product

from ctviz.aggregate.group import DEFAULT_CITATION_CAP
from ctviz.aggregate.registry import (
    DIMENSIONS,
    RELATIONS,
    DimensionValue,
    RelationSpec,
    distinct_values,
)
from ctviz.schemas.aggregate import Graph
from ctviz.schemas.citation import Citation, CitationRole
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import (
    ExcludedTerm,
    NetworkEdge,
    NetworkNode,
    NetworkSummary,
    NodeType,
)
from ctviz.schemas.trial import TrialRecord


@dataclass
class NodeTally:
    """Accumulator for one node; `trials` holds the NCT IDs it appears in."""

    label: str
    type: NodeType
    trials: set[str] = field(default_factory=set)
    citations: list[Citation] = field(default_factory=list)


@dataclass
class EdgeTally:
    """Accumulator for one undirected edge; `trials` holds the NCT IDs where both co-occur."""

    trials: set[str] = field(default_factory=set)
    citations: list[Citation] = field(default_factory=list)


@dataclass
class Exclusions:
    """Deny-listed values seen while building, with the trials that listed each one."""

    trials_by_key: dict[str, set[str]] = field(default_factory=dict)

    def record(self, key: str, nct_id: str) -> None:
        """Remember that `nct_id` listed the excluded value `key`."""
        self.trials_by_key.setdefault(key, set()).add(nct_id)


type EdgeKey = tuple[str, str]


def build_network(
    records: Sequence[TrialRecord],
    relation: Relation,
    *,
    citation_cap: int = DEFAULT_CITATION_CAP,
    max_nodes: int | None = None,
    min_edge_weight: int = 1,
) -> Graph:
    """Build an undirected graph for `relation`.

    Drugs are the study-level intervention MeSH terms (keyed by MeSH id when present).
    Sponsors are lead sponsors; conditions are the raw condition strings.
    - Values on the relation's deny-list (`RelationSpec.excluded`) never become nodes;
      they are listed in the summary with the number of trials that listed them.
    - Node `size`: number of distinct trials the entity appears in.
    - Edge `weight`: number of distinct trials in which both endpoints co-occur.
    - Edge citations list the contributing trials.
    - Readability caps (off by default): edges below `min_edge_weight` are dropped, then
      the `max_nodes` nodes with the highest weighted degree (sum of the remaining
      incident edge weights) are kept (ties: larger size, then label), with only the
      edges between kept nodes. Node sizes are not recomputed. Everything omitted is
      counted in `Graph.summary` and a warning.
    """
    spec = RELATIONS[relation]
    nodes: dict[str, NodeTally] = {}
    edges: dict[EdgeKey, EdgeTally] = {}
    exclusions = Exclusions()
    without_edges = 0

    for record in records:
        left = kept_values(record, spec.left, spec.left_type, spec, exclusions)
        right = (
            left
            if spec.same_dimension
            else kept_values(record, spec.right, spec.right_type, spec, exclusions)
        )
        add_nodes(nodes, left, spec.left_type, record.nct_id)
        if not spec.same_dimension:
            add_nodes(nodes, right, spec.right_type, record.nct_id)

        pairs = trial_pairs(spec, left, right)
        without_edges += int(not pairs)
        for (left_id, left_value), (right_id, right_value) in pairs:
            edge = edges.setdefault(edge_key(left_id, right_id), EdgeTally())
            if record.nct_id in edge.trials:
                continue
            edge.trials.add(record.nct_id)
            edge.citations.extend(primary(left_value) + primary(right_value))

    strong = {key: tally for key, tally in edges.items() if len(tally.trials) >= min_edge_weight}
    shown_ids = top_nodes_by_weighted_degree(nodes, strong, max_nodes)
    shown_edges = {key: tally for key, tally in strong.items() if set(key) <= shown_ids}
    shown_nodes = {key: tally for key, tally in nodes.items() if key in shown_ids}

    excluded = excluded_terms(spec, exclusions)
    summary = NetworkSummary(
        excluded_terms=excluded,
        trials_with_excluded_terms=len(set().union(*exclusions.trials_by_key.values())),
        max_nodes=max_nodes,
        min_edge_weight=min_edge_weight,
        nodes_total=len(nodes),
        nodes_shown=len(shown_nodes),
        edges_total=len(edges),
        edges_shown=len(shown_edges),
        edges_below_min_weight=len(edges) - len(strong),
        edges_with_omitted_node=len(strong) - len(shown_edges),
        trials_with_edges=len(trials_of(edges.values())),
        trials_without_shown_edge=len(trials_of(edges.values()) - trials_of(shown_edges.values())),
        trials_without_shown_node=len(trials_of(nodes.values()) - trials_of(shown_nodes.values())),
        edge_citation_cap=citation_cap,
    )
    return Graph(
        relation=relation,
        nodes=[to_node(key, tally, citation_cap) for key, tally in sorted_nodes(shown_nodes)],
        edges=[to_edge(key, tally, citation_cap) for key, tally in sorted_edges(shown_edges)],
        summary=summary,
        assumptions=[*spec.assumptions, *exclusion_assumptions(summary)],
        warnings=[
            *edge_warnings(spec, without_edges, len(records)),
            *trim_warnings(summary),
        ],
    )


def kept_values(
    record: TrialRecord,
    dimension: Dimension,
    node_type: NodeType,
    spec: RelationSpec,
    exclusions: Exclusions,
) -> list[DimensionValue]:
    """A trial's distinct values for one side, without the deny-listed ones."""
    denied = spec.excluded.get(node_type, {})
    kept: list[DimensionValue] = []
    for value in distinct_values(record, DIMENSIONS[dimension]):
        if value.key in denied:
            exclusions.record(value.key, record.nct_id)
        else:
            kept.append(value)
    return kept


def edge_key(left_id: str, right_id: str) -> EdgeKey:
    """Undirected edge identity: the two node ids in sorted order."""
    first, second = sorted((left_id, right_id))
    return first, second


def top_nodes_by_weighted_degree(
    nodes: dict[str, NodeTally], edges: dict[EdgeKey, EdgeTally], max_nodes: int | None
) -> set[str]:
    """Ids of the `max_nodes` nodes with the largest sum of incident edge weights.

    Weighted rather than plain degree: one umbrella trial listing many drugs gives each of
    them many weight-1 edges, while repeated co-occurrence is what "frequently" means.
    All nodes are kept when there is no cap.
    """
    if max_nodes is None:
        return set(nodes)
    degree: dict[str, int] = dict.fromkeys(nodes, 0)
    for (source, target), tally in edges.items():
        degree[source] += len(tally.trials)
        degree[target] += len(tally.trials)
    ranked = sorted(
        nodes.items(),
        key=lambda item: (-degree[item[0]], -len(item[1].trials), item[1].label, item[0]),
    )
    return {key for key, _ in ranked[:max_nodes]}


def trials_of(tallies: Iterable[NodeTally | EdgeTally]) -> set[str]:
    """Union of the trials behind a set of nodes or edges."""
    return set().union(*(tally.trials for tally in tallies))


def excluded_terms(spec: RelationSpec, exclusions: Exclusions) -> list[ExcludedTerm]:
    """Deny-listed terms that occurred, in deny-list order, with their trial counts."""
    denied = {key: entry for table in spec.excluded.values() for key, entry in table.items()}
    return [
        ExcludedTerm(
            id=key,
            label=entry.term,
            reason=entry.reason,
            trials=len(exclusions.trials_by_key[key]),
        )
        for key, entry in denied.items()
        if key in exclusions.trials_by_key
    ]


def node_id(node_type: NodeType, value: DimensionValue) -> str:
    """Stable node id; type-prefixed so a sponsor and a drug never collide."""
    return f"{node_type.value}:{value.key}"


def add_nodes(
    nodes: dict[str, NodeTally], values: list[DimensionValue], node_type: NodeType, nct_id: str
) -> None:
    """Count one trial toward each of its distinct entities, once per trial."""
    for value in values:
        tally = nodes.setdefault(node_id(node_type, value), NodeTally(value.label, node_type))
        if nct_id in tally.trials:
            continue
        tally.trials.add(nct_id)
        tally.citations.extend(value.citations)


type IdentifiedValue = tuple[str, DimensionValue]


def trial_pairs(
    spec: RelationSpec, left: list[DimensionValue], right: list[DimensionValue]
) -> list[tuple[IdentifiedValue, IdentifiedValue]]:
    """Endpoint pairs one trial contributes, each pair at most once."""
    left_ids = [(node_id(spec.left_type, value), value) for value in left]
    if spec.same_dimension:
        return list(combinations(left_ids, 2))
    right_ids = [(node_id(spec.right_type, value), value) for value in right]
    return list(product(left_ids, right_ids))


def primary(value: DimensionValue) -> list[Citation]:
    """Edges cite the values that define their endpoints, not supporting context."""
    return [c for c in value.citations if c.role == CitationRole.PRIMARY]


def sorted_nodes(nodes: dict[str, NodeTally]) -> list[tuple[str, NodeTally]]:
    """Largest nodes first, then by label, for deterministic output."""
    return sorted(nodes.items(), key=lambda item: (-len(item[1].trials), item[1].label))


def sorted_edges(edges: dict[EdgeKey, EdgeTally]) -> list[tuple[EdgeKey, EdgeTally]]:
    """Heaviest edges first, then by endpoint ids, for deterministic output."""
    return sorted(edges.items(), key=lambda item: (-len(item[1].trials), item[0]))


def to_node(node_id_: str, tally: NodeTally, citation_cap: int) -> NetworkNode:
    """Freeze a node tally, capping citations but keeping the true total."""
    return NetworkNode(
        id=node_id_,
        label=tally.label,
        type=tally.type,
        size=len(tally.trials),
        citations=tally.citations[:citation_cap],
        citation_count_total=len(tally.citations),
    )


def to_edge(pair: EdgeKey, tally: EdgeTally, citation_cap: int) -> NetworkEdge:
    """Freeze an edge tally, capping citations but keeping the true total."""
    return NetworkEdge(
        source=pair[0],
        target=pair[1],
        weight=len(tally.trials),
        citations=tally.citations[:citation_cap],
        citation_count_total=len(tally.citations),
    )


def edge_warnings(spec: RelationSpec, without_edges: int, total: int) -> list[str]:
    """Report trials that contribute no edge (invariant 3: nothing is silent)."""
    if not without_edges:
        return []
    left = DIMENSIONS[spec.left].label
    if spec.same_dimension:
        reason = f"fewer than two distinct values of {left}"
    else:
        reason = f"no {left} or no {DIMENSIONS[spec.right].label}"
    return [f"{without_edges} of {total} trials have {reason} and contribute no edges."]


def exclusion_assumptions(summary: NetworkSummary) -> list[str]:
    """State which deny-listed terms were left out and how many trials listed them."""
    if not summary.excluded_terms:
        return []
    listed = "; ".join(
        f"{term.label} ({term.id}, {term.reason.value}): {plural(term.trials, 'trial')}"
        for term in summary.excluded_terms
    )
    return [
        "Intervention MeSH terms on the registry deny-list (comparators without active "
        f"treatment, and derived terms that are not interventions) are not drawn as nodes: "
        f"{listed}. {plural(summary.trials_with_excluded_terms, 'trial')} affected; their "
        "other values are kept."
    ]


def trim_warnings(summary: NetworkSummary) -> list[str]:
    """Report the readability caps whenever they removed anything (never trim silently)."""
    if summary.nodes_shown == summary.nodes_total and summary.edges_shown == summary.edges_total:
        return []
    return [
        f"Network trimmed for readability: showing {summary.nodes_shown} of "
        f"{summary.nodes_total} nodes (highest weighted degree, cap MAX_NETWORK_NODES="
        f"{summary.max_nodes}) and {summary.edges_shown} of {summary.edges_total} edges; "
        f"{summary.edges_below_min_weight} edges below MIN_EDGE_WEIGHT="
        f"{summary.min_edge_weight} trials and {summary.edges_with_omitted_node} edges to "
        f"omitted nodes are not shown. {plural(summary.trials_without_shown_edge, 'trial')} "
        "with co-occurrences contribute no shown edge, and "
        f"{plural(summary.trials_without_shown_node, 'trial')} appear in no shown node. "
        "Node sizes still count every trial."
    ]


def plural(count: int, noun: str) -> str:
    """'1 trial', '2 trials'."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
