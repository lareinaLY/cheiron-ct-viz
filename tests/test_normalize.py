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
    UnrecognizedField,
    UnrecognizedValue,
)
from tests.factories import (
    SOURCE_PATHS,
    fixture_study,
    load_fixture_studies,
    make_raw_study,
    set_path,
)


class TestPhaseBucketUnrecognized:
    # Supersedes test_unknown_phase_value_raises: an unknown phase is a value-domain
    # failure, so the record is kept (docs/decisions.md, fault tolerance).
    def test_unknown_phase_value_is_kept_as_unrecognized(self) -> None:
        record = normalize_study(make_raw_study(phases=["PHASE5"]))
        assert record.phases == [UnrecognizedValue(raw="PHASE5")]
        assert record.phase_bucket == "Unrecognized"


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

    # Supersedes "missing phases on observational is Unknown": phase does not exist for
    # observational studies, so it is a deterministic bucket, not missing data.
    def test_missing_phases_on_observational_is_not_applicable(self) -> None:
        record = normalize_study(make_raw_study(phases=None, study_type="OBSERVATIONAL"))
        assert record.phases == []
        assert record.phase_bucket == "Not applicable (observational / expanded access)"

    def test_missing_phases_on_expanded_access_is_not_applicable(self) -> None:
        record = normalize_study(make_raw_study(phases=None, study_type="EXPANDED_ACCESS"))
        assert record.phase_bucket == "Not applicable (observational / expanded access)"

    def test_missing_phases_on_interventional_is_unknown(self) -> None:
        record = normalize_study(make_raw_study(phases=None, study_type="INTERVENTIONAL"))
        assert record.phase_bucket == "Unknown"

    def test_missing_phases_with_unrecognized_study_type_is_unknown(self) -> None:
        record = normalize_study(make_raw_study(phases=None, study_type="NEW_TYPE"))
        assert record.phase_bucket == "Unknown"

    def test_observational_with_a_phase_keeps_the_phase(self) -> None:
        record = normalize_study(make_raw_study(phases=["PHASE2"], study_type="OBSERVATIONAL"))
        assert record.phase_bucket == "Phase 2"

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

    # Supersedes test_malformed_date_raises: an unknown date format is a value-domain
    # failure, so the record is kept with the raw string (docs/decisions.md).
    def test_malformed_date_is_kept_as_unrecognized(self) -> None:
        record = normalize_study(make_raw_study(start_date={"date": "Sept 2026"}))
        assert record.nct_id == "NCT00000001"
        assert record.start_date is None
        assert record.start_date_raw == "Sept 2026"
        assert record.start_date_precision == "unrecognized"

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


PHASES_PATH = SOURCE_PATHS["phases"]
STATUS_PATH = SOURCE_PATHS["overall_status"]


class TestValueDomainFailures:
    """A value outside a known enum or date format keeps the record (as UnrecognizedValue)."""

    def test_unrecognized_phase_is_reported_with_its_index(self) -> None:
        record = normalize_study(make_raw_study(phases=["PHASE2", "PHASE5"]))
        assert record.phases == [Phase.PHASE2, UnrecognizedValue(raw="PHASE5")]
        assert record.phase_bucket == "Unrecognized"
        assert record.unrecognized_values == [
            UnrecognizedField(
                record_field="phases", field_path=f"{PHASES_PATH}[1]", value_raw="PHASE5"
            )
        ]

    def test_unrecognized_status_keeps_every_other_field(self) -> None:
        raw = fixture_study("studies_pembrolizumab_page", "NCT04993677")
        set_path(raw, STATUS_PATH, "PAUSED_FOREVER")
        record = normalize_study(raw)
        assert record.overall_status == UnrecognizedValue(raw="PAUSED_FOREVER")
        assert record.unrecognized_values == [
            UnrecognizedField(
                record_field="overall_status", field_path=STATUS_PATH, value_raw="PAUSED_FOREVER"
            )
        ]
        assert record.study_type == StudyType.INTERVENTIONAL
        assert record.lead_sponsor is not None
        assert record.lead_sponsor.agency_class == AgencyClass.INDUSTRY
        assert record.enrollment == 77
        assert record.start_date is not None

    def test_unrecognized_study_type(self) -> None:
        record = normalize_study(make_raw_study(study_type="REGISTRY_ONLY"))
        assert record.study_type == UnrecognizedValue(raw="REGISTRY_ONLY")
        assert [u.record_field for u in record.unrecognized_values] == ["study_type"]

    def test_unrecognized_lead_sponsor_class(self) -> None:
        raw = make_raw_study()
        set_path(raw, f"{SOURCE_PATHS['lead_sponsor']}.class", "CONSORTIUM")
        record = normalize_study(raw)
        assert record.lead_sponsor is not None
        assert record.lead_sponsor.name == "Acme Pharma"
        assert record.lead_sponsor.agency_class == UnrecognizedValue(raw="CONSORTIUM")
        assert record.unrecognized_values[0].field_path == (f"{SOURCE_PATHS['lead_sponsor']}.class")

    def test_unrecognized_collaborator_class_has_indexed_path(self) -> None:
        raw = make_raw_study()
        set_path(
            raw,
            SOURCE_PATHS["collaborators"],
            [{"name": "A", "class": "NIH"}, {"name": "B", "class": "CONSORTIUM"}],
        )
        record = normalize_study(raw)
        assert record.collaborators[1].agency_class == UnrecognizedValue(raw="CONSORTIUM")
        assert record.unrecognized_values[0].field_path == (
            f"{SOURCE_PATHS['collaborators']}[1].class"
        )

    def test_unrecognized_intervention_type(self) -> None:
        raw = make_raw_study(
            interventions=[
                {"type": "DRUG", "name": "pembrolizumab"},
                {"type": "GENE_EDITING", "name": "CRISPR-X"},
            ]
        )
        record = normalize_study(raw)
        assert record.interventions[0].intervention_type == "DRUG"
        assert record.interventions[1].intervention_type == UnrecognizedValue(raw="GENE_EDITING")
        assert record.interventions[1].type_raw == "GENE_EDITING"
        assert record.unrecognized_values == [
            UnrecognizedField(
                record_field="interventions",
                field_path=f"{SOURCE_PATHS['interventions']}[1].type",
                value_raw="GENE_EDITING",
            )
        ]

    def test_free_text_date_is_unrecognized(self) -> None:
        record = normalize_study(make_raw_study(start_date={"date": "soon", "type": "ACTUAL"}))
        assert record.start_date is None
        assert record.start_date_raw == "soon"
        assert record.start_date_precision == "unrecognized"
        assert record.start_date_type == "actual"
        assert record.unrecognized_values == [
            UnrecognizedField(
                record_field="start_date",
                field_path=SOURCE_PATHS["start_date"],
                value_raw="soon",
            )
        ]

    def test_impossible_calendar_date_is_unrecognized(self) -> None:
        record = normalize_study(make_raw_study(start_date={"date": "2026-02-30"}))
        assert record.start_date is None
        assert record.start_date_precision == "unrecognized"

    def test_unrecognized_date_type_keeps_the_date(self) -> None:
        record = normalize_study(make_raw_study(start_date={"date": "2026-09", "type": "PLANNED"}))
        assert record.start_date == date(2026, 9, 1)
        assert record.start_date_type == "unrecognized"
        assert record.unrecognized_values[0].value_raw == "PLANNED"

    def test_unrecognized_enrollment_type_keeps_the_count(self) -> None:
        raw = make_raw_study()
        set_path(raw, "protocolSection.designModule.enrollmentInfo", {"count": 40, "type": "X"})
        record = normalize_study(raw)
        assert record.enrollment == 40
        assert record.enrollment_type == UnrecognizedValue(raw="X")

    def test_known_values_report_nothing(self) -> None:
        for name in ("studies_pembrolizumab_page", "studies_multi_phase", "studies_multi_country"):
            for raw in load_fixture_studies(name):
                assert normalize_study(raw).unrecognized_values == []


class TestStructuralFailures:
    """A record that cannot be read at all raises SourceRecordError (skipped by the caller)."""

    @pytest.mark.parametrize("raw", [None, "NCT00000001", ["a"], 7])
    def test_study_that_is_not_an_object(self, raw: object) -> None:
        with pytest.raises(SourceRecordError) as caught:
            normalize_study(raw)
        assert caught.value.nct_id == "unknown"

    def test_missing_protocol_section(self) -> None:
        with pytest.raises(SourceRecordError, match="protocolSection") as caught:
            normalize_study({"derivedSection": {}})
        assert caught.value.nct_id == "unknown"

    def test_missing_nct_id(self) -> None:
        raw = make_raw_study()
        del raw["protocolSection"]["identificationModule"]["nctId"]
        with pytest.raises(SourceRecordError, match="NCT ID") as caught:
            normalize_study(raw)
        assert caught.value.nct_id == "unknown"

    def test_nct_id_of_wrong_type(self) -> None:
        raw = make_raw_study()
        set_path(raw, SOURCE_PATHS["nct_id"], 12345)
        with pytest.raises(SourceRecordError) as caught:
            normalize_study(raw)
        assert caught.value.nct_id == "unknown"

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (PHASES_PATH, "PHASE2"),
            (PHASES_PATH, [2]),
            (STATUS_PATH, 3),
            ("protocolSection.designModule", ["INTERVENTIONAL"]),
            ("protocolSection.statusModule.startDateStruct", "2024-01"),
            (SOURCE_PATHS["start_date"], 2024),
            (SOURCE_PATHS["conditions"], "Melanoma"),
            (SOURCE_PATHS["conditions"], ["Melanoma", None]),
            (SOURCE_PATHS["interventions"], ["pembrolizumab"]),
            (SOURCE_PATHS["interventions"], {"name": "pembrolizumab"}),
            (SOURCE_PATHS["intervention_mesh_terms"], [{"term": 5}]),
            (SOURCE_PATHS["lead_sponsor"], "Acme Pharma"),
            (SOURCE_PATHS["collaborators"], {"name": "Merck"}),
            ("protocolSection.contactsLocationsModule.locations", {"country": "France"}),
            ("protocolSection.contactsLocationsModule.locations", [{"country": 1}]),
            (SOURCE_PATHS["enrollment"], "77"),
            (SOURCE_PATHS["enrollment"], True),
            (SOURCE_PATHS["title"], ["A title"]),
        ],
    )
    def test_field_of_wrong_json_type(self, path: str, value: object) -> None:
        raw = make_raw_study()
        set_path(raw, path, value)
        with pytest.raises(SourceRecordError) as caught:
            normalize_study(raw)
        assert caught.value.nct_id == "NCT00000001"
        assert caught.value.field_path.startswith(path)

    def test_unknown_sections_and_keys_are_ignored(self) -> None:
        raw = fixture_study("studies_pembrolizumab_page", "NCT04993677")
        raw["brandNewSection"] = {"anything": [1, 2, 3]}
        raw["protocolSection"]["statusModule"]["brandNewKey"] = {"x": 1}
        record = normalize_study(raw)
        assert record.nct_id == "NCT04993677"
        assert record.unrecognized_values == []
