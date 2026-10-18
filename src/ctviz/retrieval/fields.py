"""The `fields` projection requested for every study.

Always project: full records are about 35x larger and contain personal contact data
(docs/api_findings.md §7).
"""

from __future__ import annotations

STUDY_FIELDS: tuple[str, ...] = (
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "StudyType",
    "Phase",
    "StartDate",
    "StartDateType",
    "Condition",
    "InterventionName",
    "InterventionType",
    "InterventionOtherName",
    "LeadSponsorName",
    "LeadSponsorClass",
    "CollaboratorName",
    "CollaboratorClass",
    "LocationCountry",
    "EnrollmentCount",
    "EnrollmentType",
    "ConditionMeshTerm",
    "InterventionMeshTerm",
    # Stable MeSH identifier (e.g. C582435); terms can be renamed. Verified live 2026-10-17.
    "InterventionMeshId",
)
