"""Citation: the link from a visualized datum back to the exact source value."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class CitationRole(StrEnum):
    """Why a citation is attached.

    `primary` cites the value the datum was grouped on. `supporting` cites related
    source values shown for context, with no claimed link to the primary value
    (e.g. the raw intervention names next to a study-level MeSH term).
    """

    PRIMARY = "primary"
    SUPPORTING = "supporting"


class Citation(BaseModel):
    """One source value from one study.

    `value` is the exact value at `field_path` in the raw API response. None means
    the field is absent in the source record, which is how "Unknown" buckets are cited.
    """

    nct_id: str
    field_path: str
    value: str | int | list[str] | None
    url: str
    role: CitationRole = CitationRole.PRIMARY
