"""Normalized internal trial record and the API enums it uses.

Enum values mirror `GET /studies/enums` (see docs/api_findings.md §1).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# Bucket key for a missing value. Distinct from the API's own `UNKNOWN` enum values.
UNKNOWN_BUCKET_KEY = "Unknown"

# Bucket key for a value that is present but outside the enum we know (e.g. a phase the
# API added after this code was written). Distinct from Unknown, which means missing.
UNRECOGNIZED_BUCKET_KEY = "Unrecognized"

# Phase bucket for a phase-less observational or expanded-access study: phase does not
# exist for these study types, so it is a known value derived from studyType, not missing.
PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY = "Not applicable (observational / expanded access)"


class Phase(StrEnum):
    """Trial phase as listed in `protocolSection.designModule.phases`."""

    NA = "NA"
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"


class OverallStatus(StrEnum):
    """Overall recruitment status.

    The API's `Status` enum mixes trial statuses with expanded-access statuses.
    `UNKNOWN` is a real API value and is distinct from a missing status.
    """

    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


class StudyType(StrEnum):
    """Study type from `protocolSection.designModule.studyType`."""

    EXPANDED_ACCESS = "EXPANDED_ACCESS"
    INTERVENTIONAL = "INTERVENTIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"


class AgencyClass(StrEnum):
    """Sponsor or collaborator class. `UNKNOWN` is a real API value, not "missing"."""

    NIH = "NIH"
    FED = "FED"
    OTHER_GOV = "OTHER_GOV"
    INDIV = "INDIV"
    INDUSTRY = "INDUSTRY"
    NETWORK = "NETWORK"
    AMBIG = "AMBIG"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class InterventionType(StrEnum):
    """Intervention type from `armsInterventionsModule.interventions[].type`."""

    BEHAVIORAL = "BEHAVIORAL"
    BIOLOGICAL = "BIOLOGICAL"
    COMBINATION_PRODUCT = "COMBINATION_PRODUCT"
    DEVICE = "DEVICE"
    DIAGNOSTIC_TEST = "DIAGNOSTIC_TEST"
    DIETARY_SUPPLEMENT = "DIETARY_SUPPLEMENT"
    DRUG = "DRUG"
    GENETIC = "GENETIC"
    PROCEDURE = "PROCEDURE"
    RADIATION = "RADIATION"
    OTHER = "OTHER"


class EnrollmentType(StrEnum):
    """Whether the enrollment count is actual or anticipated."""

    ACTUAL = "ACTUAL"
    ESTIMATED = "ESTIMATED"


class UnrecognizedValue(BaseModel):
    """A value outside the enum (or date format) we know, kept verbatim.

    Upstream vocabularies drift: rejecting the whole record for one new value would drop
    a trial that is still usable on every other dimension, so the field holds this
    marker instead and dimensions that use it put the trial in the Unrecognized bucket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: str


class UnrecognizedField(BaseModel):
    """Where a record holds an unrecognized value, for meta warnings and drift counts.

    `record_field` is a key of `TrialRecord.source_paths` (e.g. "phases"), or
    "intervention_name" for a missing intervention name; `field_path` is the exact JSON
    path, with list indices (e.g. "...phases[1]"). `value_raw` is "" for a missing value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_field: str
    field_path: str
    value_raw: str


class Intervention(BaseModel):
    """One intervention as listed by the sponsor, kept verbatim.

    No MeSH term is attached here: the API gives MeSH at study level only, and
    linking a term to a specific intervention would be an inference
    (see docs/decisions.md).
    """

    name_raw: str | None = Field(
        default=None,
        description="The name exactly as listed; None (or empty) when the source has none.",
    )
    type_raw: str | None = None

    @property
    def name(self) -> str:
        """The name for display; a missing name is shown as the Unrecognized marker."""
        return self.name_raw or UNRECOGNIZED_BUCKET_KEY

    @property
    def intervention_type(self) -> InterventionType | UnrecognizedValue | None:
        """`type_raw` as an enum value; a value outside the enum stays recognizable as such.

        Derived rather than stored so the raw string remains the single source value.
        """
        if self.type_raw is None:
            return None
        try:
            return InterventionType(self.type_raw)
        except ValueError:
            return UnrecognizedValue(raw=self.type_raw)


class MeshTerm(BaseModel):
    """One entry of `derivedSection.interventionBrowseModule.meshes`.

    `id` (e.g. "C582435") is the stable identifier; terms can be renamed. It is
    None only for records fetched without the `InterventionMeshId` projection.
    """

    id: str | None = None
    term: str


class CountryLocation(BaseModel):
    """One distinct country of a study, with the location row that first lists it.

    `country` is the trimmed value used as the bucket key; `value_raw` is the exact API
    string and `location_index` its index in `locations[]`, so citations quote the source.
    """

    country: str
    value_raw: str
    location_index: int


class Sponsor(BaseModel):
    """A lead sponsor or collaborator."""

    name: str
    agency_class: AgencyClass | UnrecognizedValue | None = Field(default=None, alias="class")

    model_config = {"populate_by_name": True}


class TrialRecord(BaseModel):
    """A normalized study record, the unit every aggregation counts.

    List fields keep the source order, so an index `i` into a list matches index `i`
    of the corresponding raw JSON array and can be cited as `<path>[i]`.
    Countries are deduplicated; `country_locations` keeps the first location index of each.
    """

    model_config = ConfigDict(extra="forbid")

    nct_id: str
    title: str | None = None
    overall_status: OverallStatus | UnrecognizedValue | None = None
    study_type: StudyType | UnrecognizedValue | None = None
    phases: list[Phase | UnrecognizedValue] = Field(default_factory=list)
    phase_bucket: str
    start_date: date | None = None
    start_date_raw: str | None = Field(
        default=None, description="Exact source string, e.g. '2026-09', used as the cited value."
    )
    start_date_precision: Literal["day", "month", "year", "unknown", "unrecognized"] = Field(
        default="unknown",
        description="'unrecognized': a date string in an unknown format, kept in "
        "start_date_raw with start_date None.",
    )
    start_date_type: Literal["actual", "estimated", "unknown", "unrecognized"] = "unknown"
    conditions: list[str] = Field(default_factory=list)
    interventions: list[Intervention] = Field(default_factory=list)
    intervention_mesh_terms: list[MeshTerm] = Field(default_factory=list)
    lead_sponsor: Sponsor | None = None
    collaborators: list[Sponsor] = Field(default_factory=list)
    country_locations: list[CountryLocation] = Field(
        default_factory=list, description="Distinct countries in first-seen order."
    )
    enrollment: int | UnrecognizedValue | None = None
    enrollment_type: EnrollmentType | UnrecognizedValue | None = None
    unrecognized_values: list[UnrecognizedField] = Field(
        default_factory=list,
        description="Every value kept as UnrecognizedValue (or an unrecognized date), "
        "with its exact path and raw string.",
    )
    source_paths: dict[str, str] = Field(
        default_factory=dict,
        description="Record field name -> JSON path in the raw API response, used for citations.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def countries(self) -> list[str]:
        """Distinct trimmed country names, derived from `country_locations`."""
        return [location.country for location in self.country_locations]
