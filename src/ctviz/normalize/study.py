"""Raw ClinicalTrials.gov study JSON -> TrialRecord."""

from __future__ import annotations

from typing import Any

from ctviz.schemas.trial import TrialRecord


def normalize_study(raw: dict[str, Any]) -> TrialRecord:
    """Convert one raw study (as returned in `studies[]`) into a TrialRecord.

    Rules (see docs/decisions.md):
    - `phase_bucket`: one mutually exclusive label per trial. Phases are joined in
      canonical order ("Phase 1/Phase 2"); `["NA"]` -> "Not Applicable";
      absent or empty phases -> "Unknown".
    - Start date: "YYYY-MM" -> first of the month with precision "month";
      "YYYY-MM-DD" -> precision "day"; absent -> None / "unknown".
      Date type ACTUAL/ESTIMATED -> "actual"/"estimated"; absent -> "unknown".
    - Countries: trimmed, deduplicated within the study, first-seen order; absent -> [].
    - Interventions: raw names and types kept verbatim. Intervention MeSH terms are
      taken from the study-level `derivedSection.interventionBrowseModule.meshes`
      list only, without linking them to individual interventions.
    - Absent and empty lists are treated the same.
    """
    raise NotImplementedError("normalize_study is not implemented yet")
