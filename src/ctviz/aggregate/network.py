"""Build co-occurrence networks from TrialRecords."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations, product

from ctviz.aggregate.drug_terms import hub_assumptions
from ctviz.aggregate.group import DEFAULT_CITATION_CAP, cap_citations
from ctviz.aggregate.mesh_filter import MeshVerdict
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
    ExclusionReason,
    NetworkEdge,
    NetworkNode,
    NetworkSummary,
    NodeType,
    Notice,
    NoticeCode,
    UnmatchedTerm,
)
from ctviz.schemas.trial import TrialRecord
from ctviz.text import plural, verb


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
class ScreenedTerm:
    """A value the node filter dropped, flagged or marked as a drug class, with its trials.

    `node_type` is the side the filter screened, which gives the value's node id.
    """

    label: str
    verdict: MeshVerdict
    node_type: NodeType
    trials: set[str] = field(default_factory=set)


@dataclass
class Screening:
    """Values the node filter did not keep as plain drugs, by node key."""

    terms: dict[str, ScreenedTerm] = field(default_factory=dict)

    def record(
        self, value: DimensionValue, verdict: MeshVerdict, node_type: NodeType, nct_id: str
    ) -> None:
        """Remember that `nct_id` listed `value`, which the filter judged `verdict`."""
        term = self.terms.setdefault(value.key, ScreenedTerm(value.label, verdict, node_type))
        term.trials.add(nct_id)

    def of(self, *verdicts: MeshVerdict) -> list[tuple[str, ScreenedTerm]]:
        """Recorded terms with one of `verdicts`, most trials first, then by label."""
        matching = [(key, term) for key, term in self.terms.items() if term.verdict in verdicts]
        return sorted(matching, key=lambda item: (-len(item[1].trials), item[1].label, item[0]))


type EdgeKey = tuple[str, str]

# Terms named in the exclusion assumption sentences; meta.network lists all of them.
ASSUMPTION_TERM_LIMIT = 10


def build_network(
    records: Sequence[TrialRecord],
    relation: Relation,
    *,
    citation_cap: int = DEFAULT_CITATION_CAP,
    max_nodes: int | None = None,
    min_edge_weight: int = 1,
    queried_drug: str | None = None,
) -> Graph:
    """Build an undirected graph for `relation`.

    `queried_drug` is the cohort's drug filter: for a relation with drug nodes, trials
    that matched it by name only (no MeSH term containing it) are stated as a hub
    undercount (`drug_terms.hub_assumptions`).

    Drugs are the study-level intervention MeSH terms (keyed by MeSH id when present).
    Sponsors are lead sponsors; conditions are the raw condition strings.
    - Values rejected by the relation's node filter (`RelationSpec.node_filters`: MeSH
      tree branch and comparators, see `mesh_filter`) never become nodes; they are listed
      in the summary with the number of trials that listed them, as are kept values the
      filter could not match in the MeSH trees.
    - Node `size`: number of distinct trials the entity appears in.
    - Edge `weight`: number of distinct trials in which both endpoints co-occur.
    - Edge citations list the contributing trials. Node and edge citations are capped at
      `citation_cap` primary citations (supporting ones stay with each cited trial);
      `cited_trials_total` counts the distinct trials before the cap.
    - Trials that contribute no edge are reported in two warnings: those lacking a value
      on one side in the source, and those whose values the node filter removed.
    - Readability caps (off by default): edges below `min_edge_weight` are dropped, then
      the `max_nodes` nodes with the highest weighted degree (sum of the remaining
      incident edge weights) are kept (ties: larger size, then label), with only the
      edges between kept nodes. Node sizes are not recomputed. Everything omitted is
      counted in `Graph.summary` and a warning.
    """
    spec = RELATIONS[relation]
    nodes: dict[str, NodeTally] = {}
    edges: dict[EdgeKey, EdgeTally] = {}
    screening = Screening()
    lacking_values = 0
    filtered_out = 0

    for record in records:
        left, left_raw = kept_values(record, spec.left, spec.left_type, spec, screening)
        right, right_raw = (
            (left, left_raw)
            if spec.same_dimension
            else kept_values(record, spec.right, spec.right_type, spec, screening)
        )
        add_nodes(nodes, left, spec.left_type, record.nct_id)
        if not spec.same_dimension:
            add_nodes(nodes, right, spec.right_type, record.nct_id)

        pairs = trial_pairs(spec, left, right)
        if not pairs:
            raw_pairs = left_raw >= 2 if spec.same_dimension else min(left_raw, right_raw) >= 1
            if raw_pairs:
                filtered_out += 1
            else:
                lacking_values += 1
        for (left_id, left_value), (right_id, right_value) in pairs:
            edge = edges.setdefault(edge_key(left_id, right_id), EdgeTally())
            if record.nct_id in edge.trials:
                continue
            edge.trials.add(record.nct_id)
            edge.citations.extend(primary(left_value) + primary(right_value))

    drug_class_ids = mark_drug_classes(nodes, screening)
    strong = {key: tally for key, tally in edges.items() if len(tally.trials) >= min_edge_weight}
    shown_ids = top_nodes_by_weighted_degree(nodes, strong, max_nodes)
    shown_edges = {key: tally for key, tally in strong.items() if set(key) <= shown_ids}
    shown_nodes = {key: tally for key, tally in nodes.items() if key in shown_ids}

    excluded = screening.of(MeshVerdict.COMPARATOR, MeshVerdict.NOT_IN_CHEMICALS_AND_DRUGS_BRANCH)
    summary = NetworkSummary(
        excluded_terms=[excluded_term(key, term) for key, term in excluded],
        trials_with_excluded_terms=len(set().union(*(term.trials for _, term in excluded))),
        unmatched_terms=[
            UnmatchedTerm(id=key, label=term.label, trials=len(term.trials))
            for key, term in screening.of(MeshVerdict.UNMATCHED_IN_TREE)
        ],
        drug_class_nodes_total=len(drug_class_ids),
        drug_class_nodes_shown=len(drug_class_ids & shown_ids),
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
        assumptions=[
            *(Notice.of(NoticeCode.NETWORK_NOTE, text) for text in spec.assumptions),
            *exclusion_assumptions(summary),
            *drug_class_assumptions(summary, screening),
            *(
                Notice.of(NoticeCode.NETWORK_HUB_UNDERCOUNT, text)
                for text in (
                    hub_assumptions(records, queried_drug)
                    if queried_drug is not None
                    and NodeType.DRUG in (spec.left_type, spec.right_type)
                    else []
                )
            ),
        ],
        warnings=[
            *(Notice.of(NoticeCode.NETWORK_SEMANTICS, text) for text in spec.warnings),
            *edge_warnings(spec, lacking_values, filtered_out, len(records)),
            *trim_warnings(summary),
        ],
    )


def kept_values(
    record: TrialRecord,
    dimension: Dimension,
    node_type: NodeType,
    spec: RelationSpec,
    screening: Screening,
) -> tuple[list[DimensionValue], int]:
    """A trial's distinct values for one side without the ones the node filter rejects,
    and how many distinct values it had before the filter."""
    values = distinct_values(record, DIMENSIONS[dimension])
    node_filter = spec.node_filters.get(node_type)
    if node_filter is None:
        return values, len(values)
    kept: list[DimensionValue] = []
    for value in values:
        verdict = node_filter.classify(value.key, value.label)
        if verdict is not MeshVerdict.KEPT:
            screening.record(value, verdict, node_type, record.nct_id)
        if verdict.kept:
            kept.append(value)
    return kept, len(values)


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


def mark_drug_classes(nodes: dict[str, NodeTally], screening: Screening) -> set[str]:
    """Retype drug-class nodes and return their ids; ids keep the screened side's prefix."""
    ids = {node_key_id(term.node_type, key) for key, term in screening.of(MeshVerdict.DRUG_CLASS)}
    for drug_class_id in ids:
        nodes[drug_class_id].type = NodeType.DRUG_CLASS
    return ids


def excluded_term(key: str, term: ScreenedTerm) -> ExcludedTerm:
    """Freeze a dropped term for the summary."""
    return ExcludedTerm(
        id=key, label=term.label, reason=ExclusionReason(term.verdict), trials=len(term.trials)
    )


def node_id(node_type: NodeType, value: DimensionValue) -> str:
    """Stable node id; type-prefixed so a sponsor and a drug never collide."""
    return node_key_id(node_type, value.key)


def node_key_id(node_type: NodeType, key: str) -> str:
    """Node id for a value key (see `node_id`)."""
    return f"{node_type.value}:{key}"


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
    """Freeze a node tally, capping citations but keeping the true totals."""
    return NetworkNode(
        id=node_id_,
        label=tally.label,
        type=tally.type,
        size=len(tally.trials),
        citations=cap_citations(tally.citations, citation_cap),
        cited_trials_total=len(tally.trials),
        citation_count_total=len(tally.citations),
    )


def to_edge(pair: EdgeKey, tally: EdgeTally, citation_cap: int) -> NetworkEdge:
    """Freeze an edge tally, capping citations but keeping the true totals."""
    return NetworkEdge(
        source=pair[0],
        target=pair[1],
        weight=len(tally.trials),
        citations=tally.citations[:citation_cap],
        cited_trials_total=len(tally.trials),
        citation_count_total=len(tally.citations),
    )


def edge_warnings(
    spec: RelationSpec, lacking_values: int, filtered_out: int, total: int
) -> list[Notice]:
    """Report trials that contribute no edge, by reason (invariant 3: nothing is silent).

    A trial missing a value in the source and a trial whose values the node filter
    removed are different facts: the first is the registry's data, the second our rule.
    """
    left = DIMENSIONS[spec.left].label
    warnings: list[Notice] = []
    if lacking_values:
        if spec.same_dimension:
            reason = f"fewer than two distinct values of {left}"
        else:
            reason = f"no {left} or no {DIMENSIONS[spec.right].label}"
        warnings.append(
            Notice.of(
                NoticeCode.NETWORK_MISSING_VALUES,
                f"{lacking_values} of {total} trials have {reason} and contribute no edges.",
            )
        )
    if filtered_out:
        filtered = DIMENSIONS[spec.right].label
        removed = (
            f"removed {filtered}s until fewer than two remained"
            if spec.same_dimension
            else f"removed all their {filtered}s"
        )
        warnings.append(
            Notice.of(
                NoticeCode.NETWORK_EXCLUDED_TERMS,
                f"{filtered_out} of {total} trials contribute no edges because the node "
                f"filter {removed} (see meta.network.excluded_terms).",
            )
        )
    return warnings


def exclusion_assumptions(summary: NetworkSummary) -> list[Notice]:
    """State which terms the node filter dropped or could not match, with trial counts.

    The sentence names the most frequent terms; the full lists are in `meta.network`.
    """
    statements: list[Notice] = []
    if summary.excluded_terms:
        listed = "; ".join(
            f"{term.label} ({term.id}, {term.reason.value}): {plural(term.trials, 'trial')}"
            for term in summary.excluded_terms[:ASSUMPTION_TERM_LIMIT]
        )
        statements.append(
            Notice.of(
                NoticeCode.NETWORK_EXCLUDED_TERMS,
                f"{plural(len(summary.excluded_terms), 'intervention MeSH term')} outside the "
                "Chemicals and Drugs branch, or comparators without active treatment, are not "
                f"drawn as nodes; most frequent: {listed}. "
                f"{plural(summary.trials_with_excluded_terms, 'trial')} affected; their other "
                "values are kept. Full list in meta.network.excluded_terms.",
            )
        )
    if summary.unmatched_terms:
        listed = "; ".join(
            f"{term.label} ({term.id}): {plural(term.trials, 'trial')}"
            for term in summary.unmatched_terms[:ASSUMPTION_TERM_LIMIT]
        )
        statements.append(
            Notice.of(
                NoticeCode.NETWORK_UNMATCHED_TERMS,
                f"{plural(len(summary.unmatched_terms), 'intervention MeSH term')} could not "
                "be found by name in the MeSH trees (unmatched_in_tree) and are kept as drug "
                f"nodes unchecked: {listed}. Full list in meta.network.unmatched_terms.",
            )
        )
    return statements


def drug_class_assumptions(summary: NetworkSummary, screening: Screening) -> list[Notice]:
    """State how many nodes are drug classes rather than specific drugs."""
    if not summary.drug_class_nodes_total:
        return []
    listed = "; ".join(
        f"{term.label} ({key}): {plural(len(term.trials), 'trial')}"
        for key, term in screening.of(MeshVerdict.DRUG_CLASS)[:ASSUMPTION_TERM_LIMIT]
    )
    return [
        Notice.of(
            NoticeCode.NETWORK_DRUG_CLASSES,
            f"{plural(summary.drug_class_nodes_total, 'node')} "
            f"({summary.drug_class_nodes_shown} shown) have type drug_class: their MeSH tree "
            "numbers are all in D26 (Pharmaceutical Preparations) or D27 (Chemical Actions and "
            "Uses), so they name a drug class or dosage form, not a specific drug. Most "
            f"frequent: {listed}.",
        )
    ]


def trim_warnings(summary: NetworkSummary) -> list[Notice]:
    """Report the readability caps whenever they removed anything (never trim silently).

    The message is for analysts, so it names no setting; meta.network carries the caps
    (`max_nodes`, `min_edge_weight`) and every count.
    """
    if summary.nodes_shown == summary.nodes_total and summary.edges_shown == summary.edges_total:
        return []
    nodes = (
        f"the {summary.nodes_shown:,} most connected of {summary.nodes_total:,} nodes"
        if summary.nodes_shown < summary.nodes_total
        else f"all {summary.nodes_total:,} nodes"
    )
    return [
        Notice.of(
            NoticeCode.NETWORK_TRIMMED,
            f"Network trimmed for readability: showing {nodes} and {summary.edges_shown:,} of "
            f"{summary.edges_total:,} links. Not shown: "
            f"{plural(summary.edges_below_min_weight, 'link')} seen in fewer than "
            f"{plural(summary.min_edge_weight, 'study', 'studies')}, and "
            f"{plural(summary.edges_with_omitted_node, 'link')} to nodes not shown. "
            f"{plural(summary.trials_without_shown_edge, 'study', 'studies')} with co-listed "
            f"values add no shown link, and "
            f"{plural(summary.trials_without_shown_node, 'study', 'studies')} "
            f"{verb(summary.trials_without_shown_node, 'appears', 'appear')} in no shown node. "
            "Node sizes still count every study.",
        )
    ]
