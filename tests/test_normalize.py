"""Tests for normalize.normalize_study (raw API study -> TrialRecord)."""

from __future__ import annotations

from datetime import date

import pytest

from ctviz.errors import SourceRecordError
from ctviz.normalize import normalize_study
from ctviz.schemas.trial import (
    AgencyClass,
    CountryLocation,
    Intervention,
    MeshTerm,
    OverallStatus,
    Phase,
    StudyType,
)
from tests.factories import SOURCE_PATHS, fixture_study, load_fixture_studies, make_raw_study


class TestPhaseBucketErrors:
    def test_unknown_phase_value_raises(self) -> None:
        with pytest.raises(SourceRecordError, match="PHASE5"):
            normalize_study(make_raw_study(phases=["PHASE5"]))


class TestPhaseBucket:
    def test_multi_phase_fixture_gets_combined_bucket(self) -> None:
        record = normalize_study(fixture_study("studies_multi_phase", "NCT06939036"))
        assert record.phases == [Phase.PHASE1, Phase.PHASE2]
        assert record.phase_bucket == "Phase 1/Phase 2"

    def test_single_phase(self) -> None:
        record = normalize_study(fixture_study("studies_pembrolizumab_page", "NCT03631407"))
        assert record.phase_bucket == "Phase 2"

    def test_na_is_not_applicable(self) -> None:
        record = normalize_study(make_raw_study(phases=["NA"]))
        assert record.phases == [Phase.NA]
        assert record.phase_bucket == "Not Applicable"

    def test_missing_phases_on_observational_is_unknown(self) -> None:
        record = normalize_study(make_raw_study(phases=None, study_type="OBSERVATIONAL"))
        assert record.phases == []
        assert record.phase_bucket == "Unknown"

    def test_empty_phase_list_is_unknown(self) -> None:
        assert normalize_study(make_raw_study(phases=[])).phase_bucket == "Unknown"

    def test_combined_label_uses_canonical_order(self) -> None:
        record = normalize_study(make_raw_study(phases=["PHASE3", "PHASE2"]))
        assert record.phase_bucket == "Phase 2/Phase 3"

    def test_early_phase_1_label(self) -> None:
        record = normalize_study(make_raw_study(phases=["EARLY_PHASE1"]))
        assert record.phase_bucket == "Early Phase 1"


class TestStartDate:
    def test_month_only_date_is_first_of_month(self) -> None:
        record = normalize_study(
            make_raw_study(start_date={"date": "2026-09", "type": "ESTIMATED"})
        )
        assert record.start_date == date(2026, 9, 1)
        assert record.start_date_precision == "month"
        assert record.start_date_raw == "2026-09"

    def test_full_date_has_day_precision(self) -> None:
        record = normalize_study(
            make_raw_study(start_date={"date": "2026-09-15", "type": "ACTUAL"})
        )
        assert record.start_date == date(2026, 9, 15)
        assert record.start_date_precision == "day"

    def test_missing_date(self) -> None:
        record = normalize_study(make_raw_study(start_date=None))
        assert record.start_date is None
        assert record.start_date_raw is None
        assert record.start_date_precision == "unknown"
        assert record.start_date_type == "unknown"

    def test_estimated_type(self) -> None:
        record = normalize_study(
            make_raw_study(start_date={"date": "2027-03-01", "type": "ESTIMATED"})
        )
        assert record.start_date_type == "estimated"

    def test_actual_type(self) -> None:
        record = normalize_study(
            make_raw_study(start_date={"date": "2021-10-06", "type": "ACTUAL"})
        )
        assert record.start_date_type == "actual"

    def test_year_only_date_is_first_of_year(self) -> None:
        record = normalize_study(make_raw_study(start_date={"date": "2026", "type": "ESTIMATED"}))
        assert record.start_date == date(2026, 1, 1)
        assert record.start_date_precision == "year"
        assert record.start_date_raw == "2026"

    def test_malformed_date_raises(self) -> None:
        with pytest.raises(SourceRecordError, match="NCT00000001"):
            normalize_study(make_raw_study(start_date={"date": "Sept 2026"}))

    def test_missing_type_with_date_present_is_unknown(self) -> None:
        # Legacy records (2013-2016) carry a date but no type; the fixture NCT02129556 is one.
        record = normalize_study(fixture_study("studies_multi_country", "NCT02129556"))
        assert record.start_date == date(2014, 12, 1)
        assert record.start_date_precision == "month"
        assert record.start_date_type == "unknown"


class TestCountries:
    def test_trailing_space_is_trimmed(self) -> None:
        record = normalize_study(make_raw_study(countries=["Bonaire, Saint Eustatius and Saba "]))
        assert record.countries == ["Bonaire, Saint Eustatius and Saba"]

    def test_same_country_at_three_sites_counted_once(self) -> None:
        record = normalize_study(make_raw_study(countries=["France", "France", "France"]))
        assert record.countries == ["France"]

    def test_trim_happens_before_dedup(self) -> None:
        record = normalize_study(make_raw_study(countries=["Guam", "Guam "]))
        assert record.countries == ["Guam"]

    def test_multi_country_fixture_deduped(self) -> None:
        record = normalize_study(fixture_study("studies_multi_country", "NCT02129556"))
        assert sorted(record.countries) == ["Australia", "Austria", "Belgium", "France", "Italy"]
        assert len(record.countries) == len(set(record.countries))

    def test_country_location_keeps_raw_value_and_first_index(self) -> None:
        raw = make_raw_study(countries=["France", "Guam ", "France", "Guam"])
        record = normalize_study(raw)
        assert record.country_locations == [
            CountryLocation(country="France", value_raw="France", location_index=0),
            CountryLocation(country="Guam", value_raw="Guam ", location_index=1),
        ]

    def test_missing_locations_is_empty(self) -> None:
        record = normalize_study(make_raw_study(countries=None))
        assert record.countries == []

    def test_fixture_without_locations_is_empty(self) -> None:
        record = normalize_study(fixture_study("studies_multi_phase", "NCT05973903"))
        assert record.countries == []


class TestInterventions:
    def test_raw_names_and_types_preserved_verbatim(self) -> None:
        record = normalize_study(fixture_study("studies_pembrolizumab_page", "NCT04993677"))
        assert record.interventions == [
            Intervention(name_raw="SEA-CD40", type_raw="DRUG"),
            Intervention(name_raw="pembrolizumab (KEYTRUDA®)", type_raw="DRUG"),
            Intervention(name_raw="pemetrexed", type_raw="DRUG"),
            Intervention(name_raw="carboplatin", type_raw="DRUG"),
        ]

    def test_study_level_mesh_terms_from_derived_section(self) -> None:
        record = normalize_study(fixture_study("studies_pembrolizumab_page", "NCT04993677"))
        assert record.intervention_mesh_terms == [
            MeshTerm(id="C582435", term="pembrolizumab"),
            MeshTerm(id="D000068437", term="Pemetrexed"),
            MeshTerm(id="D016190", term="Carboplatin"),
        ]

    def test_mesh_id_kept_when_projected(self) -> None:
        raw = make_raw_study(
            interventions=[{"type": "DRUG", "name": "MK-3475"}],
            intervention_meshes=[{"id": "C582435", "term": "pembrolizumab"}],
        )
        record = normalize_study(raw)
        assert record.intervention_mesh_terms == [MeshTerm(id="C582435", term="pembrolizumab")]
        assert record.interventions == [Intervention(name_raw="MK-3475", type_raw="DRUG")]

    def test_mesh_ancestors_and_browse_leaves_ignored(self) -> None:
        raw = make_raw_study(interventions=[{"type": "DRUG", "name": "Olaparib"}])
        raw["derivedSection"] = {
            "interventionBrowseModule": {
                "meshes": [{"id": "C531550", "term": "olaparib"}],
                "ancestors": [{"id": "D011743", "term": "Phthalazines"}],
                "browseLeaves": [{"id": "M1", "name": "Olaparib"}],
            }
        }
        record = normalize_study(raw)
        assert record.intervention_mesh_terms == [MeshTerm(id="C531550", term="olaparib")]

    def test_missing_derived_section_gives_no_mesh(self) -> None:
        raw = make_raw_study(interventions=[{"type": "DRUG", "name": "SEA-CD40"}])
        assert normalize_study(raw).intervention_mesh_terms == []

    def test_missing_interventions_is_empty(self) -> None:
        assert normalize_study(make_raw_study(interventions=None)).interventions == []


class TestScalarFields:
    def test_fixture_fields(self) -> None:
        record = normalize_study(fixture_study("studies_pembrolizumab_page", "NCT04993677"))
        assert record.nct_id == "NCT04993677"
        assert record.title == "A Study of SEA-CD40 Given With Other Drugs in Cancers"
        assert record.overall_status == OverallStatus.COMPLETED
        assert record.study_type == StudyType.INTERVENTIONAL
        assert record.conditions == ["Melanoma", "Carcinoma, Non-Small- Cell Lung"]
        assert record.lead_sponsor is not None
        assert record.lead_sponsor.name == "Seagen Inc."
        assert record.lead_sponsor.agency_class == AgencyClass.INDUSTRY
        assert [c.name for c in record.collaborators] == ["Merck Sharp & Dohme LLC"]
        assert record.enrollment == 77
        assert record.enrollment_type == "ACTUAL"

    def test_absent_collaborators_is_empty(self) -> None:
        record = normalize_study(fixture_study("studies_pembrolizumab_page", "NCT03631407"))
        assert record.collaborators == []


@pytest.mark.parametrize(("field", "path"), sorted(SOURCE_PATHS.items()))
def test_source_path_for_every_cited_field(field: str, path: str) -> None:
    record = normalize_study(fixture_study("studies_pembrolizumab_page", "NCT04993677"))
    assert record.source_paths[field] == path


def test_every_fixture_study_normalizes() -> None:
    for name in ("studies_pembrolizumab_page", "studies_multi_phase", "studies_multi_country"):
        for raw in load_fixture_studies(name):
            record = normalize_study(raw)
            assert record.nct_id.startswith("NCT")
