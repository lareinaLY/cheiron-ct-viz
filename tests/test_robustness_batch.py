"""Bounded warnings, value-domain intervention names, network edge reasons, display labels."""

from __future__ import annotations

from ctviz.aggregate import build_network, group_by
from ctviz.aggregate.registry import INTERVENTION_TYPE_LABELS
from ctviz.errors import SourceRecordError
from ctviz.normalize import normalize_study
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import NoticeCode
from ctviz.schemas.trial import (
    UNRECOGNIZED_BUCKET_KEY,
    AgencyClass,
    Intervention,
    InterventionType,
    Sponsor,
)
from tests.factories import make_raw_study, make_record, mesh

RADIOTHERAPY = mesh("Radiotherapy", "D011878")
PEMBROLIZUMAB = mesh("pembrolizumab", "C582435")
MERCK = Sponsor(name="Merck", agency_class=AgencyClass.INDUSTRY)


def test_source_record_error_truncates_long_values() -> None:
    error = SourceRecordError("NCT1", "path", ["x" * 500], "bad")
    shown = str(error).split(" = ", 1)[1].split(": bad")[0]
    assert len(shown) <= 121
    assert shown.endswith("…")


def test_short_values_are_shown_whole() -> None:
    assert str(SourceRecordError("NCT1", "path", "abc", "bad")) == "NCT1: path = 'abc': bad"


class TestNamelessIntervention:
    def raw(self) -> dict[str, object]:
        return make_raw_study(
            "NCT00797238",
            interventions=[{"type": "DRUG"}, {"name": "Placebo", "type": "DRUG"}],
        )

    def test_record_is_kept_with_an_unrecognized_name(self) -> None:
        record = normalize_study(self.raw())
        first, second = record.interventions
        assert first.name == UNRECOGNIZED_BUCKET_KEY
        assert first.name_raw is None
        assert second.name == "Placebo"

    def test_missing_name_is_reported_as_unrecognized(self) -> None:
        record = normalize_study(self.raw())
        (value,) = record.unrecognized_values
        assert value.record_field == "intervention_name"
        assert value.field_path.endswith("interventions[0].name")

    def test_supporting_citation_cites_the_absent_value(self) -> None:
        record = normalize_study(self.raw()).model_copy(
            update={"intervention_mesh_terms": [PEMBROLIZUMAB]}
        )
        (bucket,) = group_by([record], Dimension.INTERVENTION_MESH).buckets
        assert [c.value for c in bucket.citations] == ["pembrolizumab", None, "Placebo"]


class TestNetworkEdgeReasons:
    def test_missing_values_and_filtered_terms_are_counted_separately(self) -> None:
        records = [
            make_record("NCT1", lead_sponsor=MERCK, intervention_mesh_terms=[PEMBROLIZUMAB]),
            make_record("NCT2", lead_sponsor=MERCK),
            make_record("NCT3", lead_sponsor=MERCK, intervention_mesh_terms=[RADIOTHERAPY]),
            make_record("NCT4", lead_sponsor=MERCK, intervention_mesh_terms=[RADIOTHERAPY]),
        ]
        graph = build_network(records, Relation.SPONSOR_DRUG)
        (missing,) = [w for w in graph.warnings if w.code == NoticeCode.NETWORK_MISSING_VALUES]
        assert missing.message == (
            "1 of 4 trials have no lead sponsor or no intervention MeSH term and contribute "
            "no edges."
        )
        (filtered,) = [w for w in graph.warnings if w.code == NoticeCode.NETWORK_EXCLUDED_TERMS]
        assert filtered.message.startswith(
            "2 of 4 trials contribute no edges because the node filter removed all their "
            "intervention MeSH terms"
        )

    def test_cooccurrence_counts_trials_left_with_one_drug_after_filtering(self) -> None:
        records = [
            make_record("NCT1", intervention_mesh_terms=[PEMBROLIZUMAB, RADIOTHERAPY]),
            make_record("NCT2", intervention_mesh_terms=[PEMBROLIZUMAB]),
        ]
        graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
        (missing,) = [w for w in graph.warnings if w.code == NoticeCode.NETWORK_MISSING_VALUES]
        assert missing.message.startswith("1 of 2 trials have fewer than two distinct values")
        (filtered,) = [w for w in graph.warnings if w.code == NoticeCode.NETWORK_EXCLUDED_TERMS]
        assert filtered.message.startswith("1 of 2 trials contribute no edges because")


class TestInterventionTypeLabels:
    def test_every_type_has_a_label(self) -> None:
        assert set(INTERVENTION_TYPE_LABELS) == set(InterventionType)

    def test_key_and_citation_keep_the_code(self) -> None:
        records = [
            make_record(
                "NCT1", interventions=[Intervention(name_raw="a", type_raw="DIETARY_SUPPLEMENT")]
            )
        ]
        (bucket,) = group_by(records, Dimension.INTERVENTION_TYPE).buckets
        assert bucket.key == "DIETARY_SUPPLEMENT"
        assert bucket.label == "Dietary supplement"
        assert bucket.citations[0].value == "DIETARY_SUPPLEMENT"
