"""Raw ClinicalTrials.gov study JSON -> TrialRecord."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, Literal

from ctviz.errors import SourceRecordError
from ctviz.schemas.trial import (
    UNKNOWN_BUCKET_KEY,
    AgencyClass,
    CountryLocation,
    EnrollmentType,
    Intervention,
    MeshTerm,
    OverallStatus,
    Phase,
    Sponsor,
    StudyType,
    TrialRecord,
)

# Record field -> JSON path in the raw API response (docs/api_findings.md §3).
SOURCE_PATHS: dict[str, str] = {
    "nct_id": "protocolSection.identificationModule.nctId",
    "title": "protocolSection.identificationModule.briefTitle",
    "overall_status": "protocolSection.statusModule.overallStatus",
    "study_type": "protocolSection.designModule.studyType",
    "phases": "protocolSection.designModule.phases",
    "start_date": "protocolSection.statusModule.startDateStruct.date",
    "start_date_type": "protocolSection.statusModule.startDateStruct.type",
    "conditions": "protocolSection.conditionsModule.conditions",
    "interventions": "protocolSection.armsInterventionsModule.interventions",
    "intervention_mesh_terms": "derivedSection.interventionBrowseModule.meshes",
    "lead_sponsor": "protocolSection.sponsorCollaboratorsModule.leadSponsor",
    "collaborators": "protocolSection.sponsorCollaboratorsModule.collaborators",
    "countries": "protocolSection.contactsLocationsModule.locations[].country",
    "enrollment": "protocolSection.designModule.enrollmentInfo.count",
    "enrollment_type": "protocolSection.designModule.enrollmentInfo.type",
}

LOCATIONS_PATH = "protocolSection.contactsLocationsModule.locations"

PHASE_LABELS: dict[Phase, str] = {
    Phase.EARLY_PHASE1: "Early Phase 1",
    Phase.PHASE1: "Phase 1",
    Phase.PHASE2: "Phase 2",
    Phase.PHASE3: "Phase 3",
    Phase.PHASE4: "Phase 4",
    Phase.NA: "Not Applicable",
}

DatePrecision = Literal["day", "month", "year", "unknown"]
DateType = Literal["actual", "estimated", "unknown"]

# Precision is decided by the shape of the string; the API emits these three forms.
DATE_PATTERNS: tuple[tuple[re.Pattern[str], DatePrecision], ...] = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "day"),
    (re.compile(r"^(\d{4})-(\d{2})$"), "month"),
    (re.compile(r"^(\d{4})$"), "year"),
)

DATE_TYPES: dict[str, DateType] = {"ACTUAL": "actual", "ESTIMATED": "estimated"}


def dig(data: dict[str, Any], path: str) -> Any:
    """Return the value at a dotted path, or None if any segment is absent."""
    current: Any = data
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def parse_enum[E: StrEnum](
    enum_type: type[E], value: Any, nct_id: str, field_path: str
) -> E | None:
    """Convert an API enum string, or raise SourceRecordError for an unknown value."""
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError as error:
        raise SourceRecordError(
            nct_id, field_path, value, f"not a known {enum_type.__name__} value"
        ) from error


def phase_bucket(phases: list[Phase]) -> str:
    """One mutually exclusive label per trial; multi-phase trials get a combined label."""
    if not phases:
        return UNKNOWN_BUCKET_KEY
    canonical_order = list(PHASE_LABELS)
    ordered = sorted(set(phases), key=canonical_order.index)
    return "/".join(PHASE_LABELS[phase] for phase in ordered)


def parse_start_date(raw_date: str | None, nct_id: str) -> tuple[date | None, DatePrecision]:
    """Parse a partial ISO date; month- and year-only dates map to their first day."""
    if raw_date is None:
        return None, "unknown"
    for pattern, precision in DATE_PATTERNS:
        match = pattern.match(raw_date)
        if match is None:
            continue
        parts = [int(part) for part in match.groups()]
        year = parts[0]
        month = parts[1] if len(parts) > 1 else 1
        day = parts[2] if len(parts) > 2 else 1
        try:
            return date(year, month, day), precision
        except ValueError as error:
            raise SourceRecordError(
                nct_id, SOURCE_PATHS["start_date"], raw_date, "not a valid calendar date"
            ) from error
    raise SourceRecordError(
        nct_id, SOURCE_PATHS["start_date"], raw_date, "expected YYYY, YYYY-MM or YYYY-MM-DD"
    )


def parse_date_type(raw_type: str | None, nct_id: str) -> DateType:
    """Map ACTUAL/ESTIMATED to lower case; absent means unknown (legacy records)."""
    if raw_type is None:
        return "unknown"
    if raw_type not in DATE_TYPES:
        raise SourceRecordError(
            nct_id, SOURCE_PATHS["start_date_type"], raw_type, "expected ACTUAL or ESTIMATED"
        )
    return DATE_TYPES[raw_type]


def parse_country_locations(locations: list[dict[str, Any]]) -> list[CountryLocation]:
    """Distinct trimmed countries, each with the first location row that lists it.

    Rows without a country are not countries and are skipped; none were observed in
    the API data (docs/api_findings.md §9).
    """
    seen: dict[str, CountryLocation] = {}
    for index, location in enumerate(locations):
        value_raw = location.get("country")
        if not value_raw or not value_raw.strip():
            continue
        name = value_raw.strip()
        if name not in seen:
            seen[name] = CountryLocation(country=name, value_raw=value_raw, location_index=index)
    return list(seen.values())


def parse_interventions(raw_interventions: list[dict[str, Any]], nct_id: str) -> list[Intervention]:
    """Raw interventions in source order; a missing name is an error, not a skip.

    Skipping would shift indices and break `interventions[j]` citations.
    """
    interventions = []
    for index, raw in enumerate(raw_interventions):
        name = raw.get("name")
        if not name:
            path = f"{SOURCE_PATHS['interventions']}[{index}].name"
            raise SourceRecordError(nct_id, path, name, "intervention has no name")
        interventions.append(Intervention(name_raw=name, type_raw=raw.get("type")))
    return interventions


def parse_mesh_terms(raw_meshes: list[dict[str, Any]], nct_id: str) -> list[MeshTerm]:
    """Study-level intervention MeSH terms, in source order."""
    terms = []
    for index, raw in enumerate(raw_meshes):
        term = raw.get("term")
        if not term:
            path = f"{SOURCE_PATHS['intervention_mesh_terms']}[{index}].term"
            raise SourceRecordError(nct_id, path, term, "MeSH entry has no term")
        terms.append(MeshTerm(id=raw.get("id"), term=term))
    return terms


def parse_sponsor(raw: dict[str, Any] | None, nct_id: str, field_path: str) -> Sponsor | None:
    """A sponsor with a name; a sponsor entry without a name is treated as absent."""
    if not raw or not raw.get("name"):
        return None
    agency_class = parse_enum(AgencyClass, raw.get("class"), nct_id, f"{field_path}.class")
    return Sponsor(name=raw["name"], agency_class=agency_class)


def normalize_study(raw: dict[str, Any]) -> TrialRecord:
    """Convert one raw study (as returned in `studies[]`) into a TrialRecord.

    Rules (see docs/decisions.md):
    - `phase_bucket`: one mutually exclusive label per trial. Phases are joined in
      canonical order ("Phase 1/Phase 2"); `["NA"]` -> "Not Applicable";
      absent or empty phases -> "Unknown".
    - Start date: "YYYY-MM" -> first of the month with precision "month";
      "YYYY" -> January 1 with precision "year"; "YYYY-MM-DD" -> precision "day";
      absent -> None / "unknown".
      Date type ACTUAL/ESTIMATED -> "actual"/"estimated"; absent -> "unknown".
    - Countries: trimmed, deduplicated within the study, first-seen order; absent -> [].
    - Interventions: raw names and types kept verbatim. Intervention MeSH terms are
      taken from the study-level `derivedSection.interventionBrowseModule.meshes`
      list only, without linking them to individual interventions.
    - Absent and empty lists are treated the same.

    Raises SourceRecordError for values that cannot be interpreted (unknown enum
    values, malformed dates) rather than guessing.
    """
    nct_id = dig(raw, SOURCE_PATHS["nct_id"])
    if not nct_id:
        raise SourceRecordError("<missing>", SOURCE_PATHS["nct_id"], nct_id, "study has no NCT ID")

    phases = [
        Phase(parse_enum(Phase, value, nct_id, SOURCE_PATHS["phases"]))
        for value in dig(raw, SOURCE_PATHS["phases"]) or []
    ]
    start_date_raw = dig(raw, SOURCE_PATHS["start_date"])
    start_date, start_date_precision = parse_start_date(start_date_raw, nct_id)
    sponsor_path = SOURCE_PATHS["lead_sponsor"]
    collaborator_path = SOURCE_PATHS["collaborators"]

    return TrialRecord(
        nct_id=nct_id,
        title=dig(raw, SOURCE_PATHS["title"]),
        overall_status=parse_enum(
            OverallStatus,
            dig(raw, SOURCE_PATHS["overall_status"]),
            nct_id,
            SOURCE_PATHS["overall_status"],
        ),
        study_type=parse_enum(
            StudyType, dig(raw, SOURCE_PATHS["study_type"]), nct_id, SOURCE_PATHS["study_type"]
        ),
        phases=phases,
        phase_bucket=phase_bucket(phases),
        start_date=start_date,
        start_date_raw=start_date_raw,
        start_date_precision=start_date_precision,
        start_date_type=parse_date_type(dig(raw, SOURCE_PATHS["start_date_type"]), nct_id),
        conditions=list(dig(raw, SOURCE_PATHS["conditions"]) or []),
        interventions=parse_interventions(dig(raw, SOURCE_PATHS["interventions"]) or [], nct_id),
        intervention_mesh_terms=parse_mesh_terms(
            dig(raw, SOURCE_PATHS["intervention_mesh_terms"]) or [], nct_id
        ),
        lead_sponsor=parse_sponsor(dig(raw, sponsor_path), nct_id, sponsor_path),
        collaborators=[
            sponsor
            for index, entry in enumerate(dig(raw, collaborator_path) or [])
            if (sponsor := parse_sponsor(entry, nct_id, f"{collaborator_path}[{index}]"))
        ],
        country_locations=parse_country_locations(dig(raw, LOCATIONS_PATH) or []),
        enrollment=dig(raw, SOURCE_PATHS["enrollment"]),
        enrollment_type=parse_enum(
            EnrollmentType,
            dig(raw, SOURCE_PATHS["enrollment_type"]),
            nct_id,
            SOURCE_PATHS["enrollment_type"],
        ),
        source_paths=dict(SOURCE_PATHS),
    )
