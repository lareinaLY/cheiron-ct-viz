"""Startup check: does the API's enum vocabulary still match ours?

Values the API added since this code was written are handled per record (kept as
Unrecognized, see `normalize`), but hearing about them once at startup is cheaper than
finding them in response warnings. The check is advisory: it never blocks startup.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.retrieval import EnumDrift
from ctviz.schemas.trial import AgencyClass, InterventionType, OverallStatus, Phase, StudyType

logger = logging.getLogger(__name__)

# API enum type name (GET /studies/enums `type`) -> our enum (docs/api_findings.md §1).
KNOWN_ENUMS: dict[str, type[StrEnum]] = {
    "Phase": Phase,
    "Status": OverallStatus,
    "StudyType": StudyType,
    "AgencyClass": AgencyClass,
    "InterventionType": InterventionType,
}


def api_enum_values(payload: Any) -> dict[str, set[str]]:
    """Enum type -> values from a /studies/enums body; ValueError if it is malformed."""
    if not isinstance(payload, list):
        raise ValueError(f"/studies/enums: expected a list, got {type(payload).__name__}")
    values: dict[str, set[str]] = {}
    for entry in payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("values"), list):
            raise ValueError(f"/studies/enums: malformed entry {entry!r:.200}")
        items = entry["values"]
        if not all(isinstance(item, dict) and isinstance(item.get("value"), str) for item in items):
            raise ValueError(f"/studies/enums: malformed values in {entry.get('type')!r}")
        values[str(entry.get("type"))] = {item["value"] for item in items}
    return values


def enum_drift(payload: Any) -> EnumDrift:
    """Compare a /studies/enums body with KNOWN_ENUMS.

    Only values the API has and we lack are drift: a value we know that the API no
    longer lists cannot appear in records, so it is harmless.
    """
    api_values = api_enum_values(payload)
    new_values = {
        type_name: sorted(api_values[type_name] - {member.value for member in enum_type})
        for type_name, enum_type in KNOWN_ENUMS.items()
        if type_name in api_values
    }
    return EnumDrift(
        new_values={name: values for name, values in new_values.items() if values},
        missing_types=[name for name in KNOWN_ENUMS if name not in api_values],
    )


def drift_message(drift: EnumDrift) -> str:
    """The single log line describing drift."""
    parts = [f"{name}: {', '.join(values)}" for name, values in drift.new_values.items()]
    parts.extend(f"{name}: no longer listed by the API" for name in drift.missing_types)
    return (
        "ClinicalTrials.gov enums differ from ctviz's (records with new values are kept and "
        f"bucketed as Unrecognized): {'; '.join(parts)}"
    )


async def check_enum_drift(client: CtGovClient) -> EnumDrift | None:
    """Fetch /studies/enums once and log one WARNING line if the API has values we lack.

    Failure-tolerant by design: any failure (network, HTTP, malformed body) is logged and
    None is returned, because this check must never stop the server from starting.
    """
    try:
        drift = enum_drift(await client.fetch_enums())
    except Exception as error:
        # Deliberately broad: the check is advisory and runs as a background task, where an
        # unexpected exception would otherwise vanish unreported.
        logger.warning("Enum drift check skipped: %s: %s", type(error).__name__, error)
        return None
    if drift.has_drift:
        logger.warning("%s", drift_message(drift))
    else:
        logger.info("Enum drift check: API enums match ctviz's")
    return drift
