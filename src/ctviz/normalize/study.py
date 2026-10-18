"""Raw ClinicalTrials.gov study JSON -> TrialRecord."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Literal, NoReturn

from pydantic import ValidationError

from ctviz.errors import SourceRecordError
from ctviz.schemas.trial import (
    UNKNOWN_BUCKET_KEY,
    UNRECOGNIZED_BUCKET_KEY,
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
    UnrecognizedField,
    UnrecognizedValue,
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

# NCT ID reported for a record that has none (or none we can read).
UNKNOWN_NCT_ID = "unknown"

PHASE_LABELS: dict[Phase, str] = {
    Phase.EARLY_PHASE1: "Early Phase 1",
    Phase.PHASE1: "Phase 1",
    Phase.PHASE2: "Phase 2",
    Phase.PHASE3: "Phase 3",
    Phase.PHASE4: "Phase 4",
    Phase.NA: "Not Applicable",
}

DatePrecision = Literal["day", "month", "year", "unknown", "unrecognized"]
DateType = Literal["actual", "estimated", "unknown", "unrecognized"]

# Precision is decided by the shape of the string; the API emits these three forms.
DATE_PATTERNS: tuple[tuple[re.Pattern[str], DatePrecision], ...] = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "day"),
    (re.compile(r"^(\d{4})-(\d{2})$"), "month"),
    (re.compile(r"^(\d{4})$"), "year"),
)

DATE_TYPES: dict[str, DateType] = {"ACTUAL": "actual", "ESTIMATED": "estimated"}


JSON_TYPE_NAMES: dict[type, str] = {
    dict: "an object",
    list: "a list",
    str: "a string",
    int: "an integer",
}


def json_type_name(value: object) -> str:
    """The JSON name of a decoded value's type, for error messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, float):
        return "a number"
    return JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def has_json_type(value: object, expected: type) -> bool:
    """isinstance, except that JSON booleans are not integers."""
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)


@dataclass
class StudyReader:
    """Typed access to one raw study (a plain dict, never a strict model).

    Two failure classes (docs/decisions.md):
    - structural: a value of the wrong JSON type (or an object where a path expects one is
      something else) raises SourceRecordError, and the caller skips the record;
    - value-domain: a string outside a known enum is returned as UnrecognizedValue and
      recorded in `unrecognized`, so the record stays usable on every other field.
    Absent keys and unknown extra keys are both fine: only the paths we read are checked.
    """

    raw: dict[str, Any]
    nct_id: str
    unrecognized: list[UnrecognizedField] = field(default_factory=list)

    def get(self, path: str, expected: type) -> Any:
        """The value at a dotted path, or None if any segment is absent."""
        current: Any = self.raw
        walked: list[str] = []
        for segment in path.split("."):
            if not isinstance(current, dict):
                self.fail(
                    ".".join(walked), current, f"expected an object, got {json_type_name(current)}"
                )
            walked.append(segment)
            current = current.get(segment)
            if current is None:
                return None
        return self.check(current, expected, path)

    def check(self, value: Any, expected: type, path: str) -> Any:
        """Return `value` if it is None or of the expected JSON type, else fail."""
        if value is not None and not has_json_type(value, expected):
            self.fail(
                path, value, f"expected {JSON_TYPE_NAMES[expected]}, got {json_type_name(value)}"
            )
        return value

    def items(self, path: str, item_type: type) -> list[Any]:
        """A list at `path` whose items all have `item_type`; absent means empty."""
        values = self.get(path, list) or []
        for index, value in enumerate(values):
            if not has_json_type(value, item_type):
                self.fail(
                    f"{path}[{index}]",
                    value,
                    f"expected {JSON_TYPE_NAMES[item_type]}, got {json_type_name(value)}",
                )
        return values

    def enum[E: StrEnum](
        self, enum_type: type[E], value: Any, path: str, record_field: str
    ) -> E | UnrecognizedValue | None:
        """An API enum string; an unknown string is kept as UnrecognizedValue."""
        if self.check(value, str, path) is None:
            return None
        try:
            return enum_type(value)
        except ValueError:
            return self.keep_unrecognized(value, path, record_field)

    def keep_unrecognized(self, value: str, path: str, record_field: str) -> UnrecognizedValue:
        """Record a value-domain failure and return its marker."""
        self.unrecognized.append(
            UnrecognizedField(record_field=record_field, field_path=path, value_raw=value)
        )
        return UnrecognizedValue(raw=value)

    def fail(self, path: str, value: Any, problem: str) -> NoReturn:
        """Raise the structural failure for this record."""
        raise SourceRecordError(self.nct_id, path, value, problem)


def phase_bucket(phases: list[Phase | UnrecognizedValue]) -> str:
    """One mutually exclusive label per trial; multi-phase trials get a combined label.

    A trial listing any unrecognized phase cannot be named faithfully, so it goes to the
    Unrecognized bucket as a whole.
    """
    if not phases:
        return UNKNOWN_BUCKET_KEY
    known = [phase for phase in phases if isinstance(phase, Phase)]
    if len(known) < len(phases):
        return UNRECOGNIZED_BUCKET_KEY
    canonical_order = list(PHASE_LABELS)
    ordered = sorted(set(known), key=canonical_order.index)
    return "/".join(PHASE_LABELS[phase] for phase in ordered)


def parse_start_date(reader: StudyReader) -> tuple[date | None, DatePrecision]:
    """Parse a partial ISO date; month- and year-only dates map to their first day.

    A string in another format, or an impossible calendar date, is a value-domain
    failure: the date is None with precision "unrecognized" and the raw string is kept.
    """
    path = SOURCE_PATHS["start_date"]
    raw_date = reader.get(path, str)
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
        except ValueError:
            break
    reader.keep_unrecognized(raw_date, path, "start_date")
    return None, "unrecognized"


def parse_date_type(reader: StudyReader) -> DateType:
    """Map ACTUAL/ESTIMATED to lower case; absent means unknown (legacy records)."""
    path = SOURCE_PATHS["start_date_type"]
    raw_type = reader.get(path, str)
    if raw_type is None:
        return "unknown"
    if raw_type not in DATE_TYPES:
        reader.keep_unrecognized(raw_type, path, "start_date_type")
        return "unrecognized"
    return DATE_TYPES[raw_type]


def parse_country_locations(reader: StudyReader) -> list[CountryLocation]:
    """Distinct trimmed countries, each with the first location row that lists it.

    Rows without a country are not countries and are skipped; none were observed in
    the API data (docs/api_findings.md §9).
    """
    seen: dict[str, CountryLocation] = {}
    for index, location in enumerate(reader.items(LOCATIONS_PATH, dict)):
        value_raw = reader.check(location.get("country"), str, f"{LOCATIONS_PATH}[{index}].country")
        if not value_raw or not value_raw.strip():
            continue
        name = value_raw.strip()
        if name not in seen:
            seen[name] = CountryLocation(country=name, value_raw=value_raw, location_index=index)
    return list(seen.values())


def parse_interventions(reader: StudyReader) -> list[Intervention]:
    """Raw interventions in source order; a missing name is an error, not a skip.

    Skipping would shift indices and break `interventions[j]` citations. An unknown
    type is kept verbatim and recorded as unrecognized.
    """
    base = SOURCE_PATHS["interventions"]
    interventions = []
    for index, raw in enumerate(reader.items(base, dict)):
        name = reader.check(raw.get("name"), str, f"{base}[{index}].name")
        if not name:
            reader.fail(f"{base}[{index}].name", name, "intervention has no name")
        type_path = f"{base}[{index}].type"
        type_raw = reader.check(raw.get("type"), str, type_path)
        intervention = Intervention(name_raw=name, type_raw=type_raw)
        if isinstance(intervention.intervention_type, UnrecognizedValue):
            reader.keep_unrecognized(type_raw, type_path, "interventions")
        interventions.append(intervention)
    return interventions


def parse_mesh_terms(reader: StudyReader) -> list[MeshTerm]:
    """Study-level intervention MeSH terms, in source order."""
    base = SOURCE_PATHS["intervention_mesh_terms"]
    terms = []
    for index, raw in enumerate(reader.items(base, dict)):
        term = reader.check(raw.get("term"), str, f"{base}[{index}].term")
        if not term:
            reader.fail(f"{base}[{index}].term", term, "MeSH entry has no term")
        mesh_id = reader.check(raw.get("id"), str, f"{base}[{index}].id")
        terms.append(MeshTerm(id=mesh_id, term=term))
    return terms


def parse_sponsor(
    reader: StudyReader, raw: dict[str, Any] | None, field_path: str, record_field: str
) -> Sponsor | None:
    """A sponsor with a name; a sponsor entry without a name is treated as absent."""
    if raw is None:
        return None
    name = reader.check(raw.get("name"), str, f"{field_path}.name")
    if not name:
        return None
    agency_class = reader.enum(AgencyClass, raw.get("class"), f"{field_path}.class", record_field)
    return Sponsor(name=name, agency_class=agency_class)


def parse_collaborators(reader: StudyReader) -> list[Sponsor]:
    """Collaborators with a name, in source order."""
    base = SOURCE_PATHS["collaborators"]
    return [
        sponsor
        for index, entry in enumerate(reader.items(base, dict))
        if (sponsor := parse_sponsor(reader, entry, f"{base}[{index}]", "collaborators"))
    ]


def read_nct_id(raw: dict[str, Any]) -> str:
    """The study's NCT ID; a study without a readable one cannot be cited or deduplicated."""
    if raw.get("protocolSection") is None:
        raise SourceRecordError(
            UNKNOWN_NCT_ID, "protocolSection", None, "study has no protocolSection"
        )
    path = SOURCE_PATHS["nct_id"]
    nct_id = StudyReader(raw, UNKNOWN_NCT_ID).get(path, str)
    if not nct_id:
        raise SourceRecordError(UNKNOWN_NCT_ID, path, nct_id, "study has no NCT ID")
    return nct_id


def normalize_study(raw: object) -> TrialRecord:
    """Convert one raw study (as returned in `studies[]`) into a TrialRecord.

    Rules (see docs/decisions.md):
    - `phase_bucket`: one mutually exclusive label per trial. Phases are joined in
      canonical order ("Phase 1/Phase 2"); `["NA"]` -> "Not Applicable";
      absent or empty phases -> "Unknown"; any unrecognized phase -> "Unrecognized".
    - Start date: "YYYY-MM" -> first of the month with precision "month";
      "YYYY" -> January 1 with precision "year"; "YYYY-MM-DD" -> precision "day";
      absent -> None / "unknown"; any other string -> None / "unrecognized".
      Date type ACTUAL/ESTIMATED -> "actual"/"estimated"; absent -> "unknown";
      any other string -> "unrecognized".
    - Countries: trimmed, deduplicated within the study, first-seen order; absent -> [].
    - Interventions: raw names and types kept verbatim. Intervention MeSH terms are
      taken from the study-level `derivedSection.interventionBrowseModule.meshes`
      list only, without linking them to individual interventions.
    - Absent and empty lists are treated the same; unknown sections and keys are ignored.
    - Enum values we do not know (phase, status, study type, sponsor and collaborator
      class, intervention type, enrollment type) are kept as UnrecognizedValue and listed
      in `unrecognized_values`, never guessed and never a reason to drop the record.

    Raises SourceRecordError for structural failures only: not an object, no
    protocolSection or NCT ID, a value of the wrong JSON type, or a nameless
    intervention or MeSH entry (skipping list entries would shift citation indices).
    """
    if not isinstance(raw, dict):
        raise SourceRecordError(
            UNKNOWN_NCT_ID, "<study>", raw, f"expected an object, got {json_type_name(raw)}"
        )
    nct_id = read_nct_id(raw)
    reader = StudyReader(raw, nct_id)
    phases_path = SOURCE_PATHS["phases"]
    phases = [
        phase
        for index, value in enumerate(reader.items(phases_path, str))
        if (phase := reader.enum(Phase, value, f"{phases_path}[{index}]", "phases")) is not None
    ]
    start_date, start_date_precision = parse_start_date(reader)
    sponsor_path = SOURCE_PATHS["lead_sponsor"]

    try:
        return TrialRecord(
            nct_id=nct_id,
            title=reader.get(SOURCE_PATHS["title"], str),
            overall_status=reader.enum(
                OverallStatus,
                reader.get(SOURCE_PATHS["overall_status"], str),
                SOURCE_PATHS["overall_status"],
                "overall_status",
            ),
            study_type=reader.enum(
                StudyType,
                reader.get(SOURCE_PATHS["study_type"], str),
                SOURCE_PATHS["study_type"],
                "study_type",
            ),
            phases=phases,
            phase_bucket=phase_bucket(phases),
            start_date=start_date,
            start_date_raw=reader.get(SOURCE_PATHS["start_date"], str),
            start_date_precision=start_date_precision,
            start_date_type=parse_date_type(reader),
            conditions=reader.items(SOURCE_PATHS["conditions"], str),
            interventions=parse_interventions(reader),
            intervention_mesh_terms=parse_mesh_terms(reader),
            lead_sponsor=parse_sponsor(
                reader, reader.get(sponsor_path, dict), sponsor_path, "lead_sponsor"
            ),
            collaborators=parse_collaborators(reader),
            country_locations=parse_country_locations(reader),
            enrollment=reader.get(SOURCE_PATHS["enrollment"], int),
            enrollment_type=reader.enum(
                EnrollmentType,
                reader.get(SOURCE_PATHS["enrollment_type"], str),
                SOURCE_PATHS["enrollment_type"],
                "enrollment_type",
            ),
            unrecognized_values=reader.unrecognized,
            source_paths=dict(SOURCE_PATHS),
        )
    except ValidationError as error:
        # Every field is type-checked above, so this is a gap in those checks; it is still
        # one unreadable record, not a reason to fail the whole request.
        raise SourceRecordError(
            nct_id, "<record>", None, f"TrialRecord rejected it: {error}"
        ) from error
