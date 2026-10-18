"""Unrecognized values in aggregation (the Unrecognized bucket) and their meta warnings."""

from __future__ import annotations

from datetime import date

from ctviz.aggregate import UNKNOWN_BUCKET_KEY, group_by, group_cohorts
from ctviz.normalize import normalize_study, unrecognized_warnings
from ctviz.schemas.aggregate import Bucket, GroupResult
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import UNRECOGNIZED_BUCKET_KEY, TrialRecord
from tests.factories import SOURCE_PATHS, make_raw_study, set_path

AS_OF = date(2026, 10, 17)


def bucket(result: GroupResult, key: str) -> Bucket:
    matches = [b for b in result.buckets if b.key == key]
    assert len(matches) == 1, f"expected exactly one bucket {key!r}, got {len(matches)}"
    return matches[0]


def study_with(nct_id: str, path: str, value: object, **fields: object) -> TrialRecord:
    raw = make_raw_study(nct_id, **fields)  # type: ignore[arg-type]
    set_path(raw, path, value)
    return normalize_study(raw)


class TestUnrecognizedBucket:
    def test_unrecognized_phase_has_its_own_bucket_citing_the_raw_list(self) -> None:
        records = [
            normalize_study(make_raw_study("NCT1", phases=["PHASE2"])),
            normalize_study(make_raw_study("NCT2", phases=["PHASE2", "PHASE5"])),
            normalize_study(make_raw_study("NCT3")),
        ]
        result = group_by(records, Dimension.PHASE, as_of=AS_OF)
        unrecognized = bucket(result, UNRECOGNIZED_BUCKET_KEY)
        assert unrecognized.count == 1
        assert unrecognized.is_unrecognized is True
        assert unrecognized.is_unknown is False
        citation = unrecognized.citations[0]
        assert citation.nct_id == "NCT2"
        assert citation.field_path == SOURCE_PATHS["phases"]
        assert citation.value == ["PHASE2", "PHASE5"]
        assert bucket(result, UNKNOWN_BUCKET_KEY).is_unrecognized is False

    def test_unrecognized_sorts_after_known_and_before_unknown(self) -> None:
        records = [
            normalize_study(make_raw_study("NCT1", phases=["PHASE5"])),
            normalize_study(make_raw_study("NCT2", phases=["PHASE5"])),
            normalize_study(make_raw_study("NCT3", phases=["PHASE1"])),
            normalize_study(make_raw_study("NCT4")),
        ]
        result = group_by(records, Dimension.PHASE, as_of=AS_OF)
        assert [b.key for b in result.buckets] == ["Phase 1", "Unrecognized", "Unknown"]

    def test_unrecognized_status(self) -> None:
        path = SOURCE_PATHS["overall_status"]
        records = [
            study_with("NCT1", path, "PAUSED_FOREVER"),
            study_with("NCT2", path, "RECRUITING"),
        ]
        result = group_by(records, Dimension.OVERALL_STATUS, as_of=AS_OF)
        unrecognized = bucket(result, UNRECOGNIZED_BUCKET_KEY)
        assert unrecognized.count == 1
        assert unrecognized.citations[0].value == "PAUSED_FOREVER"
        assert unrecognized.citations[0].field_path == path
        assert bucket(result, "RECRUITING").count == 1

    def test_unrecognized_sponsor_class(self) -> None:
        path = f"{SOURCE_PATHS['lead_sponsor']}.class"
        records = [study_with("NCT1", path, "CONSORTIUM"), study_with("NCT2", path, "NIH")]
        result = group_by(records, Dimension.SPONSOR_CLASS, as_of=AS_OF)
        unrecognized = bucket(result, UNRECOGNIZED_BUCKET_KEY)
        assert unrecognized.citations[0].value == "CONSORTIUM"
        assert unrecognized.citations[0].field_path == path
        assert bucket(result, "NIH").count == 1

    def test_unrecognized_intervention_type_next_to_known_types(self) -> None:
        record = normalize_study(
            make_raw_study(
                "NCT1",
                interventions=[
                    {"type": "DRUG", "name": "pembrolizumab"},
                    {"type": "GENE_EDITING", "name": "CRISPR-X"},
                ],
            )
        )
        result = group_by([record], Dimension.INTERVENTION_TYPE, as_of=AS_OF)
        assert bucket(result, "DRUG").count == 1
        unrecognized = bucket(result, UNRECOGNIZED_BUCKET_KEY)
        assert unrecognized.citations[0].value == "GENE_EDITING"
        assert unrecognized.citations[0].field_path == (f"{SOURCE_PATHS['interventions']}[1].type")

    def test_unrecognized_date_is_unknown_on_temporal_dimensions(self) -> None:
        records = [
            normalize_study(make_raw_study("NCT1", start_date={"date": "soon"})),
            normalize_study(make_raw_study("NCT2", start_date={"date": "2024-05"})),
        ]
        for dimension in (Dimension.START_YEAR, Dimension.START_MONTH):
            result = group_by(records, dimension, as_of=AS_OF)
            unknown = bucket(result, UNKNOWN_BUCKET_KEY)
            assert unknown.count == 1
            assert unknown.citations[0].nct_id == "NCT1"
            assert unknown.citations[0].value == "soon"
            assert all(not b.is_unrecognized for b in result.buckets)

    def test_unrecognized_date_type_counts_as_untyped(self) -> None:
        record = normalize_study(
            make_raw_study("NCT1", start_date={"date": "2024-05", "type": "PLANNED"})
        )
        result = group_by([record], Dimension.START_YEAR, as_of=AS_OF)
        year = bucket(result, "2024")
        assert year.count == 1
        assert year.estimated_count == 0
        assert year.unknown_date_type_count == 1

    def test_top_n_never_omits_unrecognized(self) -> None:
        path = SOURCE_PATHS["overall_status"]
        records = [
            study_with("NCT1", path, "RECRUITING"),
            study_with("NCT2", path, "RECRUITING"),
            study_with("NCT3", path, "COMPLETED"),
            study_with("NCT4", path, "PAUSED_FOREVER"),
        ]
        grouped = group_cohorts([("all", records)], Dimension.OVERALL_STATUS, top_n=1, as_of=AS_OF)
        assert grouped.key_order == ["RECRUITING", UNRECOGNIZED_BUCKET_KEY]
        assert grouped.top_n is not None
        assert grouped.top_n.omitted_keys == ["COMPLETED"]


class TestUnrecognizedWarnings:
    def test_one_warning_per_distinct_value_with_its_count(self) -> None:
        records = [
            normalize_study(make_raw_study("NCT1", phases=["PHASE5"])),
            normalize_study(make_raw_study("NCT2", phases=["PHASE5"])),
            normalize_study(make_raw_study("NCT3", phases=["PHASE2", "PHASE5", "PHASE6"])),
            normalize_study(make_raw_study("NCT4", phases=["PHASE2"])),
        ]
        warnings = unrecognized_warnings(records)
        assert len(warnings) == 2
        phase5, phase6 = warnings
        assert "'PHASE5'" in phase5
        assert "3 trials" in phase5
        assert all(nct in phase5 for nct in ("NCT1", "NCT2", "NCT3"))
        assert SOURCE_PATHS["phases"] in phase5
        assert "Unrecognized" in phase5
        assert "'PHASE6'" in phase6
        assert "1 trial " in phase6

    def test_same_raw_value_in_different_fields_is_reported_separately(self) -> None:
        raw = make_raw_study("NCT1", study_type="PAUSED")
        set_path(raw, SOURCE_PATHS["overall_status"], "PAUSED")
        warnings = unrecognized_warnings([normalize_study(raw)])
        assert len(warnings) == 2

    def test_a_trial_listing_a_value_twice_counts_once(self) -> None:
        record = normalize_study(
            make_raw_study(
                "NCT1",
                interventions=[
                    {"type": "GENE_EDITING", "name": "A"},
                    {"type": "GENE_EDITING", "name": "B"},
                ],
            )
        )
        (warning,) = unrecognized_warnings([record])
        assert "1 trial " in warning

    def test_unrecognized_date_warning_names_the_unknown_bucket(self) -> None:
        record = normalize_study(make_raw_study("NCT1", start_date={"date": "soon"}))
        (warning,) = unrecognized_warnings([record])
        assert "'soon'" in warning
        assert "'Unknown'" in warning

    def test_no_warnings_for_known_values(self) -> None:
        assert unrecognized_warnings([normalize_study(make_raw_study(phases=["PHASE1"]))]) == []
