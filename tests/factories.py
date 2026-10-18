"""Builders for raw API studies and normalized records used across tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctviz.schemas.plan import Cohort, Filters, QueryPlan
from ctviz.schemas.trial import MeshTerm, TrialRecord

FIXTURES_DIR = Path(__file__).parent / "fixtures"

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


def load_fixture_studies(name: str) -> list[dict[str, Any]]:
    """Return the raw `studies` list from a captured fixture file."""
    payload = json.loads((FIXTURES_DIR / f"{name}.json").read_text())
    return payload["response"]["studies"]


def fixture_study(name: str, nct_id: str) -> dict[str, Any]:
    """Return one raw study from a fixture by NCT ID."""
    for study in load_fixture_studies(name):
        if study["protocolSection"]["identificationModule"]["nctId"] == nct_id:
            return study
    raise KeyError(f"{nct_id} not in fixture {name}")


def make_raw_study(
    nct_id: str = "NCT00000001",
    *,
    phases: list[str] | None = None,
    start_date: dict[str, str] | None = None,
    countries: list[str] | None = None,
    interventions: list[dict[str, Any]] | None = None,
    intervention_meshes: list[dict[str, str]] | None = None,
    study_type: str = "INTERVENTIONAL",
) -> dict[str, Any]:
    """Build a minimal raw study. A None argument omits the key, as the API does."""
    design: dict[str, Any] = {"studyType": study_type}
    if phases is not None:
        design["phases"] = phases
    status: dict[str, Any] = {"overallStatus": "COMPLETED"}
    if start_date is not None:
        status["startDateStruct"] = start_date
    protocol: dict[str, Any] = {
        "identificationModule": {"nctId": nct_id, "briefTitle": f"Study {nct_id}"},
        "statusModule": status,
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme Pharma", "class": "INDUSTRY"}},
        "designModule": design,
    }
    if countries is not None:
        protocol["contactsLocationsModule"] = {"locations": [{"country": c} for c in countries]}
    if interventions is not None:
        protocol["armsInterventionsModule"] = {"interventions": interventions}
    study: dict[str, Any] = {"protocolSection": protocol}
    if intervention_meshes is not None:
        study["derivedSection"] = {"interventionBrowseModule": {"meshes": intervention_meshes}}
    return study


def make_record(nct_id: str, **fields: Any) -> TrialRecord:
    """Build a TrialRecord directly, defaulting to an Unknown-phase record."""
    fields.setdefault("phase_bucket", "Unknown")
    fields.setdefault("source_paths", dict(SOURCE_PATHS))
    return TrialRecord(nct_id=nct_id, **fields)


def mesh(term: str, mesh_id: str | None = None) -> MeshTerm:
    """Shorthand for a MeshTerm."""
    return MeshTerm(id=mesh_id, term=term)


def make_plan(cohort_count: int = 1, **fields: Any) -> QueryPlan:
    """Build a QueryPlan with `cohort_count` drug cohorts."""
    cohorts = [
        Cohort(label=f"cohort {i}", filters=Filters(drug=f"drug{i}")) for i in range(cohort_count)
    ]
    return QueryPlan(cohorts=cohorts, **fields)
