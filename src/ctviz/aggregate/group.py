"""Group TrialRecords by one dimension into counted, cited buckets."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ctviz.aggregate.registry import (
    DIMENSIONS,
    UNKNOWN_BUCKET_KEY,
    BucketOrder,
    DimensionSpec,
    DimensionValue,
    cite,
    distinct_values,
    path_for,
)
from ctviz.schemas.aggregate import Bucket, GroupResult
from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import TrialRecord

DEFAULT_CITATION_CAP = 20


@dataclass
class BucketTally:
    """Mutable accumulator for one bucket while records are scanned."""

    key: str
    label: str
    is_unknown: bool
    rank: tuple[int, ...]
    count: int = 0
    estimated_count: int = 0
    citations: list[Citation] = field(default_factory=list)

    def add(self, value: DimensionValue) -> None:
        """Count one trial into this bucket."""
        self.count += 1
        self.estimated_count += int(value.estimated)
        self.citations.extend(value.citations)


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
    spec = DIMENSIONS[dimension]
    as_of = as_of or date.today()
    tallies: dict[str, BucketTally] = {}
    overlap = False

    for record in records:
        values = distinct_values(record, spec) or [unknown_value(record, spec)]
        if len(values) > 1 and not spec.can_overlap:
            raise ValueError(
                f"{record.nct_id}: dimension {dimension} is declared single-valued "
                f"but produced {len(values)} values"
            )
        overlap = overlap or len(values) > 1
        for value in values:
            tally = tallies.get(value.key)
            if tally is None:
                tally = BucketTally(value.key, value.label, value.is_unknown, value.rank)
                tallies[value.key] = tally
            tally.add(value)

    current_period = spec.period_key(as_of) if spec.period_key else None
    buckets = [
        to_bucket(tally, citation_cap, incomplete=tally.key == current_period)
        for tally in sort_tallies(list(tallies.values()), spec.order)
    ]
    return GroupResult(
        dimension=dimension,
        buckets=buckets,
        buckets_overlap=overlap,
        assumptions=list(spec.assumptions) + overlap_assumptions(spec, overlap, len(records)),
        warnings=bucket_warnings(spec, buckets, len(records), as_of),
    )


def unknown_value(record: TrialRecord, spec: DimensionSpec) -> DimensionValue:
    """The Unknown bucket entry for a record lacking the value; cites the absent field."""
    citation = cite(record, path_for(record, spec)(None), None)
    return DimensionValue(
        key=UNKNOWN_BUCKET_KEY, label=UNKNOWN_BUCKET_KEY, citations=(citation,), is_unknown=True
    )


def sort_tallies(tallies: list[BucketTally], order: BucketOrder) -> list[BucketTally]:
    """Sort buckets by the dimension's order, always placing Unknown last."""
    sort_keys = {
        BucketOrder.BY_COUNT: lambda t: (-t.count, t.label),
        BucketOrder.BY_KEY: lambda t: (t.key,),
        BucketOrder.BY_RANK: lambda t: (t.rank, t.key),
    }
    known = sorted((t for t in tallies if not t.is_unknown), key=sort_keys[order])
    return known + [t for t in tallies if t.is_unknown]


def to_bucket(tally: BucketTally, citation_cap: int, *, incomplete: bool) -> Bucket:
    """Freeze a tally into a Bucket, capping citations but keeping the true total."""
    return Bucket(
        key=tally.key,
        label=tally.label,
        count=tally.count,
        estimated_count=tally.estimated_count,
        incomplete=incomplete,
        is_unknown=tally.is_unknown,
        citations=tally.citations[:citation_cap],
        citation_count_total=len(tally.citations),
    )


def overlap_assumptions(spec: DimensionSpec, overlap: bool, total: int) -> list[str]:
    """Explain why bucket counts may exceed the number of trials."""
    if not overlap:
        return []
    return [
        f"A trial counts once in each {spec.label} it lists, so bucket counts can add up "
        f"to more than the {total} trials."
    ]


def bucket_warnings(
    spec: DimensionSpec, buckets: list[Bucket], total: int, as_of: date
) -> list[str]:
    """Report Unknown, estimated and in-progress buckets (invariant 3: nothing is silent)."""
    warnings = []
    unknown = sum(b.count for b in buckets if b.is_unknown)
    if unknown:
        warnings.append(
            f"{unknown} of {total} trials have no {spec.label}; "
            f"they are counted in the '{UNKNOWN_BUCKET_KEY}' bucket."
        )
    estimated = sum(b.estimated_count for b in buckets)
    if estimated:
        warnings.append(f"{estimated} trials are counted using an estimated {spec.label}.")
    warnings.extend(
        f"Bucket '{b.key}' is still in progress as of {as_of.isoformat()}; its count may grow."
        for b in buckets
        if b.incomplete
    )
    return warnings
