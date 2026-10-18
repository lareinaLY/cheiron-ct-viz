"""Build co-occurrence networks from TrialRecords."""

from __future__ import annotations

from collections.abc import Sequence
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
from ctviz.schemas.plan import Relation
from ctviz.schemas.response import NetworkEdge, NetworkNode, NodeType
from ctviz.schemas.trial import TrialRecord


@dataclass
class NodeTally:
    """Accumulator for one node."""

    label: str
    type: NodeType
    size: int = 0
    citations: list[Citation] = field(default_factory=list)


@dataclass
class EdgeTally:
    """Accumulator for one undirected edge."""

    weight: int = 0
    citations: list[Citation] = field(default_factory=list)


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
    spec = RELATIONS[relation]
    nodes: dict[str, NodeTally] = {}
    edges: dict[tuple[str, str], EdgeTally] = {}
    without_edges = 0

    for record in records:
        left = distinct_values(record, DIMENSIONS[spec.left])
        right = left if spec.same_dimension else distinct_values(record, DIMENSIONS[spec.right])
        add_nodes(nodes, left, spec.left_type)
        if not spec.same_dimension:
            add_nodes(nodes, right, spec.right_type)

        pairs = trial_pairs(spec, left, right)
        without_edges += int(not pairs)
        for (left_id, left_value), (right_id, right_value) in pairs:
            edge = edges.setdefault(tuple(sorted((left_id, right_id))), EdgeTally())
            edge.weight += 1
            edge.citations.extend(primary(left_value) + primary(right_value))

    return Graph(
        relation=relation,
        nodes=[to_node(node_id, tally, citation_cap) for node_id, tally in sorted_nodes(nodes)],
        edges=[to_edge(pair, tally, citation_cap) for pair, tally in sorted_edges(edges)],
        assumptions=list(spec.assumptions),
        warnings=edge_warnings(spec, without_edges, len(records)),
    )


def node_id(node_type: NodeType, value: DimensionValue) -> str:
    """Stable node id; type-prefixed so a sponsor and a drug never collide."""
    return f"{node_type.value}:{value.key}"


def add_nodes(
    nodes: dict[str, NodeTally], values: list[DimensionValue], node_type: NodeType
) -> None:
    """Count one trial toward each of its distinct entities."""
    for value in values:
        tally = nodes.setdefault(node_id(node_type, value), NodeTally(value.label, node_type))
        tally.size += 1
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
    return sorted(nodes.items(), key=lambda item: (-item[1].size, item[1].label))


def sorted_edges(
    edges: dict[tuple[str, str], EdgeTally],
) -> list[tuple[tuple[str, str], EdgeTally]]:
    """Heaviest edges first, then by endpoint ids, for deterministic output."""
    return sorted(edges.items(), key=lambda item: (-item[1].weight, item[0]))


def to_node(node_id_: str, tally: NodeTally, citation_cap: int) -> NetworkNode:
    """Freeze a node tally, capping citations but keeping the true total."""
    return NetworkNode(
        id=node_id_,
        label=tally.label,
        type=tally.type,
        size=tally.size,
        citations=tally.citations[:citation_cap],
        citation_count_total=len(tally.citations),
    )


def to_edge(pair: tuple[str, str], tally: EdgeTally, citation_cap: int) -> NetworkEdge:
    """Freeze an edge tally, capping citations but keeping the true total."""
    return NetworkEdge(
        source=pair[0],
        target=pair[1],
        weight=tally.weight,
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
