"""Normalized internal trial record and the API enums it uses.

Enum values mirror `GET /studies/enums` (see docs/api_findings.md §1).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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


class EnrollmentType(StrEnum):
    """Whether the enrollment count is actual or anticipated."""

    ACTUAL = "ACTUAL"
    ESTIMATED = "ESTIMATED"


class Intervention(BaseModel):
    """One intervention as listed by the sponsor, kept verbatim.

    No MeSH term is attached here: the API gives MeSH at study level only, and
    linking a term to a specific intervention would be an inference
    (see docs/decisions.md).
    """

    name_raw: str
    type_raw: str | None = None


class MeshTerm(BaseModel):
    """One entry of `derivedSection.interventionBrowseModule.meshes`.

    `id` (e.g. "C582435") is the stable identifier; terms can be renamed. It is
    None only for records fetched without the `InterventionMeshId` projection.
    """

    id: str | None = None
    term: str


class Sponsor(BaseModel):
    """A lead sponsor or collaborator."""

    name: str
    agency_class: AgencyClass | None = Field(default=None, alias="class")

    model_config = {"populate_by_name": True}


class TrialRecord(BaseModel):
    """A normalized study record, the unit every aggregation counts.

    List fields keep the source order, so an index `i` into a list matches index `i`
    of the corresponding raw JSON array and can be cited as `<path>[i]`.
    Exception: `countries` is deduplicated and therefore cited without an index.
    """

    nct_id: str
    title: str | None = None
    overall_status: OverallStatus | None = None
    study_type: StudyType | None = None
    phases: list[Phase] = Field(default_factory=list)
    phase_bucket: str
    start_date: date | None = None
    start_date_raw: str | None = Field(
        default=None, description="Exact source string, e.g. '2026-09', used as the cited value."
    )
    start_date_precision: Literal["day", "month", "unknown"] = "unknown"
    start_date_type: Literal["actual", "estimated", "unknown"] = "unknown"
    conditions: list[str] = Field(default_factory=list)
    interventions: list[Intervention] = Field(default_factory=list)
    intervention_mesh_terms: list[MeshTerm] = Field(default_factory=list)
    lead_sponsor: Sponsor | None = None
    collaborators: list[Sponsor] = Field(default_factory=list)
    countries: list[str] = Field(
        default_factory=list, description="Trimmed and deduplicated within the study."
    )
    enrollment: int | None = None
    enrollment_type: EnrollmentType | None = None
    source_paths: dict[str, str] = Field(
        default_factory=dict,
        description="Record field name -> JSON path in the raw API response, used for citations.",
    )
