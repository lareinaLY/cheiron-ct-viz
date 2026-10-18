"""Drug co-occurrence is co-listing in one trial record, not combination therapy."""

from __future__ import annotations

from ctviz.aggregate import build_network
from ctviz.aggregate.registry import RELATIONS
from ctviz.schemas.plan import Cohort, Filters, QueryPlan, Relation
from ctviz.spec.builder import chart_title
from tests.factories import make_record, mesh, messages

PEMBROLIZUMAB = mesh("pembrolizumab", "C582435")
CARBOPLATIN = mesh("Carboplatin", "D016190")
HUB_NOTE = (
    "1 trials match pembrolizumab by name only and have no pembrolizumab MeSH term; "
    "they add no edges to the hub."
)


def drug_plan(relation: Relation) -> QueryPlan:
    return QueryPlan(
        cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
        relation=relation,
    )


def test_title_says_co_listing_is_not_co_administration() -> None:
    assert chart_title(drug_plan(Relation.DRUG_DRUG_COOCCURRENCE)) == (
        "Drugs listed in the same trial record as pembrolizumab "
        "(any arm; co-listing is not co-administration)"
    )


def test_other_relation_titles_are_unchanged() -> None:
    assert chart_title(drug_plan(Relation.SPONSOR_DRUG)) == (
        "Lead sponsor-drug network for pembrolizumab"
    )
    assert chart_title(drug_plan(Relation.CONDITION_DRUG)) == (
        "Condition-drug network for pembrolizumab"
    )


def test_planner_description_does_not_promise_combinations() -> None:
    description = RELATIONS[Relation.DRUG_DRUG_COOCCURRENCE].description.lower()
    assert "combination" not in description
    assert "same trial record" in description


def test_drug_drug_network_always_warns_about_arms_and_study_types() -> None:
    graph = build_network(
        [make_record("NCT1", intervention_mesh_terms=[PEMBROLIZUMAB, CARBOPLATIN])],
        Relation.DRUG_DRUG_COOCCURRENCE,
    )
    standing = [w for w in messages(graph.warnings) if "co-administ" in w]
    assert len(standing) == 1
    for phrase in ("comparator arms", "either-or", "observational"):
        assert phrase in standing[0]


def test_other_relations_have_no_co_listing_warning() -> None:
    graph = build_network(
        [make_record("NCT1", intervention_mesh_terms=[PEMBROLIZUMAB, CARBOPLATIN])],
        Relation.SPONSOR_DRUG,
    )
    assert not any("co-administ" in w for w in messages(graph.warnings))


def test_name_only_matches_are_stated_as_a_hub_undercount() -> None:
    records = [
        make_record("NCT1", intervention_mesh_terms=[PEMBROLIZUMAB, CARBOPLATIN]),
        make_record("NCT2", intervention_mesh_terms=[CARBOPLATIN]),
    ]
    graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE, queried_drug="pembrolizumab")
    assert HUB_NOTE in messages(graph.assumptions)


def test_no_hub_note_when_every_trial_has_the_term() -> None:
    records = [make_record("NCT1", intervention_mesh_terms=[PEMBROLIZUMAB, CARBOPLATIN])]
    graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE, queried_drug="Pembrolizumab")
    assert not any("by name only" in a for a in messages(graph.assumptions))


def test_no_hub_in_the_cohort_is_stated_not_guessed() -> None:
    records = [make_record("NCT1", intervention_mesh_terms=[PEMBROLIZUMAB, CARBOPLATIN])]
    graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE, queried_drug="Keytruda")
    assert any(
        "No intervention MeSH term contains 'Keytruda'" in a for a in messages(graph.assumptions)
    )
