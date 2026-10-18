"""Meta warnings for values kept as Unrecognized (upstream vocabulary drift)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ctviz.schemas.trial import UNKNOWN_BUCKET_KEY, UNRECOGNIZED_BUCKET_KEY, TrialRecord

EXAMPLE_NCT_IDS = 3

# What happens downstream to a trial with an unrecognized value in each record field.
CONSEQUENCES: dict[str, str] = {
    "phases": f"phase charts count them in the '{UNRECOGNIZED_BUCKET_KEY}' bucket",
    "overall_status": f"status charts count them in the '{UNRECOGNIZED_BUCKET_KEY}' bucket",
    "study_type": (
        f"if they also list no phase, phase charts count them in '{UNKNOWN_BUCKET_KEY}'"
    ),
    "lead_sponsor": f"sponsor class charts count them in the '{UNRECOGNIZED_BUCKET_KEY}' bucket",
    "collaborators": "collaborator class is not used for grouping",
    "interventions": (
        f"intervention type charts count them in the '{UNRECOGNIZED_BUCKET_KEY}' bucket"
    ),
    "start_date": f"start year and start month charts count them in '{UNKNOWN_BUCKET_KEY}'",
    "intervention_name": (
        f"the intervention is shown as '{UNRECOGNIZED_BUCKET_KEY}' and its supporting "
        "citation cites the missing value"
    ),
    "start_date_type": "they are counted in their period but marked neither actual nor estimated",
    "enrollment_type": "they are binned by enrollment count but not marked as estimated",
    "enrollment": (
        "negative or fractional counts have no enrollment bin, so enrollment charts count "
        f"them in the '{UNRECOGNIZED_BUCKET_KEY}' bucket"
    ),
}

LIST_INDEX = re.compile(r"\[\d+\]")


@dataclass
class ValueTally:
    """Trials sharing one unrecognized value in one field."""

    field_path: str
    trials: list[str] = field(default_factory=list)


def unrecognized_warnings(records: Sequence[TrialRecord]) -> list[str]:
    """One warning per distinct (field, raw value), with its trial count and example NCT IDs.

    A trial listing the same value twice (e.g. two interventions of one new type) counts
    once. Warnings follow first-seen order.
    """
    tallies: dict[tuple[str, str], ValueTally] = {}
    for record in records:
        for value in record.unrecognized_values:
            path = LIST_INDEX.sub("[]", value.field_path)
            tally = tallies.setdefault((value.record_field, value.value_raw), ValueTally(path))
            if record.nct_id not in tally.trials:
                tally.trials.append(record.nct_id)
    return [
        unrecognized_warning(record_field, value_raw, tally)
        for (record_field, value_raw), tally in tallies.items()
    ]


def unrecognized_warning(record_field: str, value_raw: str, tally: ValueTally) -> str:
    """The warning for one unrecognized value."""
    count = len(tally.trials)
    trials = f"{count} trial" if count == 1 else f"{count} trials"
    examples = ", ".join(tally.trials[:EXAMPLE_NCT_IDS])
    more = ", ..." if count > EXAMPLE_NCT_IDS else ""
    if record_field == "intervention_name":
        return (
            f"Missing intervention name at {tally.field_path} in {trials} ({examples}{more}). "
            f"These trials are kept and counted on every dimension; "
            f"{CONSEQUENCES[record_field]}."
        )
    kind = "date format" if record_field == "start_date" else "API enum value"
    return (
        f"Unrecognized value {value_raw!r} at {tally.field_path} in {trials} ({examples}{more}): "
        f"not a known {kind}, so the API may have changed. These trials are kept and "
        f"counted on every other dimension; {CONSEQUENCES[record_field]}."
    )
