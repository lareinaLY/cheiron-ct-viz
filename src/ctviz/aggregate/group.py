"""Group TrialRecords by one dimension into counted, cited buckets."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ctviz.schemas.aggregate import GroupResult
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import TrialRecord

DEFAULT_CITATION_CAP = 20
UNKNOWN_BUCKET_KEY = "Unknown"


def group_by(
    records: Sequence[TrialRecord],
    dimension: Dimension,
    *,
    citation_cap: int = DEFAULT_CITATION_CAP,
    as_of: date | None = None,
) -> GroupResult:
    """Count trials per value of `dimension`.

    Rules:
    - Each bucket keeps at most `citation_cap` citations; `citation_count_total` is
      the true number of citations for the bucket.
    - Records missing the value go to the "Unknown" bucket (`is_unknown=True`). It is
      never merged into another bucket, including the API's own `UNKNOWN` enum value.
    - Multi-valued dimensions (country, condition, intervention_mesh) count a trial once
      per distinct value and set `buckets_overlap=True`.
    - Temporal dimensions count estimated dates and report them in `estimated_count`.
      The bucket for the period containing `as_of` (default: today) is `incomplete`.
    - intervention_mesh: primary citation is `...meshes[i].term`; the study's raw
      intervention names are attached as supporting citations, with no claimed link.
    """
    raise NotImplementedError("group_by is not implemented yet")
