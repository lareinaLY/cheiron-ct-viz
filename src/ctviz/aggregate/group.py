"""Group TrialRecords by one dimension into counted, cited buckets."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ctviz.aggregate.registry import (
    DIMENSIONS,
    UNKNOWN_BUCKET_KEY,
    BucketOrder,
    DimensionKind,
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
    is_unrecognized: bool
    rank: tuple[int, ...]
    count: int = 0
    estimated_count: int = 0
    unknown_date_type_count: int = 0
    citations: list[Citation] = field(default_factory=list)

    def add(self, value: DimensionValue) -> None:
        """Count one trial into this bucket."""
        self.count += 1
        self.estimated_count += int(value.estimated)
        self.unknown_date_type_count += int(value.unknown_date_type)
        self.citations.extend(value.citations)

    @classmethod
    def first_of(cls, value: DimensionValue) -> BucketTally:
        """An empty tally for the bucket `value` belongs to."""
        return cls(value.key, value.label, value.is_unknown, value.is_unrecognized, value.rank)


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
    - Values outside the known API enum go to the "Unrecognized" bucket
      (`is_unrecognized=True`), never to Unknown; dates in an unrecognized format have no
      period and go to Unknown. The pipeline warns about each unrecognized value.
    - Multi-valued dimensions (country, condition, intervention_mesh) count a trial once
      per distinct value and set `buckets_overlap=True`.
    - Temporal dimensions count estimated dates and report them in `estimated_count`.
      Dates with no ACTUAL/ESTIMATED type are counted too, reported per bucket and in
      total in `unknown_date_type_count`, with a warning. A trial with no date at all is
      in Unknown and is not counted there.
      The bucket for the period containing `as_of` (default: today) is `incomplete`;
      buckets for later periods are `is_future` (projections), with one warning.
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
                tally = BucketTally.first_of(value)
                tallies[value.key] = tally
            tally.add(value)

    current_period = spec.period_key(as_of) if spec.period_key else None
    buckets = [
        to_bucket(
            tally,
            citation_cap,
            incomplete=tally.key == current_period,
            is_future=is_future_period(tally, current_period),
        )
        for tally in sort_tallies(list(tallies.values()), spec.order)
    ]
    temporal = spec.kind == DimensionKind.TEMPORAL
    return GroupResult(
        dimension=dimension,
        buckets=buckets,
        buckets_overlap=overlap,
        estimated_count=sum(b.estimated_count for b in buckets),
        unknown_date_type_count=(
            sum(b.unknown_date_type_count for b in buckets) if temporal else None
        ),
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
    """Sort buckets by the dimension's order, then Unrecognized, always placing Unknown last."""
    sort_keys = {
        BucketOrder.BY_COUNT: lambda t: (-t.count, t.label),
        BucketOrder.BY_KEY: lambda t: (t.key,),
        BucketOrder.BY_RANK: lambda t: (t.rank, t.key),
    }
    known = sorted(
        (t for t in tallies if not (t.is_unknown or t.is_unrecognized)), key=sort_keys[order]
    )
    unrecognized = [t for t in tallies if t.is_unrecognized]
    return known + unrecognized + [t for t in tallies if t.is_unknown]


def is_future_period(tally: BucketTally, current_period: str | None) -> bool:
    """True when the bucket's period starts after the period containing as_of.

    Period keys ('2026', '2026-09') are zero-padded, so string order is time order.
    """
    if current_period is None or tally.is_unknown or tally.is_unrecognized:
        return False
    return tally.key > current_period


def to_bucket(
    tally: BucketTally, citation_cap: int, *, incomplete: bool, is_future: bool
) -> Bucket:
    """Freeze a tally into a Bucket, capping citations but keeping the true total."""
    return Bucket(
        key=tally.key,
        label=tally.label,
        count=tally.count,
        estimated_count=tally.estimated_count,
        unknown_date_type_count=tally.unknown_date_type_count,
        incomplete=incomplete,
        is_future=is_future,
        is_unknown=tally.is_unknown,
        is_unrecognized=tally.is_unrecognized,
        citations=tally.citations[:citation_cap],
        citation_count_total=len(tally.citations),
        rank=tally.rank,
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
    """Report Unknown, estimated, in-progress and future buckets (invariant 3)."""
    warnings = []
    unknown = sum(b.count for b in buckets if b.is_unknown)
    if unknown and spec.unknown_warning is not None:
        warnings.append(spec.unknown_warning.format(unknown=unknown, total=total))
    elif unknown:
        warnings.append(
            f"{unknown} of {total} trials have no {spec.label}; "
            f"they are counted in the '{UNKNOWN_BUCKET_KEY}' bucket."
        )
    # None (not determined on the count route) is stated as an assumption, not warned on.
    estimated = sum(b.estimated_count or 0 for b in buckets)
    if estimated:
        warnings.append(f"{estimated} trials are counted using an estimated {spec.label}.")
    untyped = sum(b.unknown_date_type_count or 0 for b in buckets)
    if untyped:
        warnings.append(
            f"{untyped} trials have a {spec.label} with no ACTUAL/ESTIMATED type (legacy "
            "records); they are counted but marked neither actual nor estimated."
        )
    warnings.extend(
        f"Bucket '{b.key}' is still in progress as of {as_of.isoformat()}; its count may grow."
        for b in buckets
        if b.incomplete
    )
    future = [b for b in buckets if b.is_future]
    if future:
        warnings.append(
            f"{sum(b.count for b in future)} trials have {spec.label}s in the future "
            f"(estimated); buckets {future[0].key}… are projections, not activity."
        )
    return warnings
