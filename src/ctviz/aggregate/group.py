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
    EstimateKind,
    cite,
    distinct_values,
    path_for,
)
from ctviz.schemas.aggregate import Bucket, GroupResult
from ctviz.schemas.citation import Citation, CitationRole
from ctviz.schemas.plan import Dimension
from ctviz.schemas.response import Notice, NoticeCode
from ctviz.schemas.trial import TrialRecord
from ctviz.text import plural, verb

DEFAULT_CITATION_CAP = 20


@dataclass
class BucketTally:
    """Mutable accumulator for one bucket while records are scanned."""

    key: str
    label: str
    is_unknown: bool
    is_unrecognized: bool
    rank: tuple[int, ...]
    is_not_applicable: bool = False
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
        return cls(
            value.key,
            value.label,
            value.is_unknown,
            value.is_unrecognized,
            value.rank,
            value.is_not_applicable,
        )


def group_by(
    records: Sequence[TrialRecord],
    dimension: Dimension,
    *,
    citation_cap: int = DEFAULT_CITATION_CAP,
    as_of: date | None = None,
) -> GroupResult:
    """Count trials per value of `dimension`.

    Rules:
    - Each bucket keeps at most `citation_cap` primary citations, plus the supporting
      citations of the trials those cite (`cap_citations`); `cited_trials_total` is the
      number of distinct trials cited and `citation_count_total` the number of citations,
      both before the cap.
    - Records missing the value go to the "Unknown" bucket (`is_unknown=True`). It is
      never merged into another bucket, including the API's own `UNKNOWN` enum value.
    - Values outside the known API enum go to the "Unrecognized" bucket
      (`is_unrecognized=True`), never to Unknown; dates in an unrecognized format have no
      period and go to Unknown. The pipeline warns about each unrecognized value.
    - Multi-valued dimensions (country, condition, intervention_mesh) count a trial once
      per distinct value and set `buckets_overlap=True`.
    - Dimensions with an `estimate_kind` count estimated values in `estimated_count`;
      others report it as None (no estimate concept, not zero). Start-date dimensions
      also count dates with no ACTUAL/ESTIMATED type, per bucket and in total in
      `unknown_date_type_count`, with a warning. A trial with no date at all is in
      Unknown and is not counted there.
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
            spec,
            incomplete=tally.key == current_period,
            is_future=is_future_period(tally, current_period),
        )
        for tally in sort_tallies(list(tallies.values()), spec.order)
    ]
    return GroupResult(
        dimension=dimension,
        buckets=buckets,
        buckets_overlap=overlap,
        estimated_count=(
            sum(b.estimated_count or 0 for b in buckets) if spec.estimate_kind else None
        ),
        unknown_date_type_count=(
            sum(b.unknown_date_type_count or 0 for b in buckets)
            if spec.estimate_kind == EstimateKind.DATE
            else None
        ),
        assumptions=[
            *dimension_notes(spec),
            *overlap_assumptions(spec, overlap, len(records)),
        ],
        warnings=bucket_warnings(spec, buckets, len(records), as_of),
    )


def dimension_notes(spec: DimensionSpec) -> list[Notice]:
    """The dimension's standing assumptions."""
    return [Notice.of(NoticeCode.DIMENSION_NOTE, text) for text in spec.assumptions]


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
    tally: BucketTally,
    citation_cap: int,
    spec: DimensionSpec,
    *,
    incomplete: bool,
    is_future: bool,
) -> Bucket:
    """Freeze a tally into a Bucket, capping citations but keeping the true totals.

    Estimate counts are None on a dimension without an estimate concept, and the
    untyped-date count only exists for start dates.
    """
    return Bucket(
        key=tally.key,
        label=tally.label,
        count=tally.count,
        estimated_count=tally.estimated_count if spec.estimate_kind is not None else None,
        unknown_date_type_count=(
            tally.unknown_date_type_count if spec.estimate_kind == EstimateKind.DATE else None
        ),
        incomplete=incomplete,
        is_future=is_future,
        is_unknown=tally.is_unknown,
        is_unrecognized=tally.is_unrecognized,
        is_not_applicable=tally.is_not_applicable,
        citations=cap_citations(tally.citations, citation_cap),
        cited_trials_total=len({citation.nct_id for citation in tally.citations}),
        citation_count_total=len(tally.citations),
        rank=tally.rank,
    )


def cap_citations(citations: list[Citation], cap: int) -> list[Citation]:
    """The first `cap` primary citations, plus the supporting citations of the trials they cite.

    The cap counts what the datum is grouped on; supporting context (e.g. the raw
    intervention names next to a MeSH term) stays with each cited trial instead of using
    up the cap, so a capped bucket still cites `cap` trials.
    """
    primary = [
        index for index, citation in enumerate(citations) if citation.role == CitationRole.PRIMARY
    ]
    kept = set(primary[:cap])
    cited = {citations[index].nct_id for index in kept}
    return [
        citation
        for index, citation in enumerate(citations)
        if index in kept or (citation.role != CitationRole.PRIMARY and citation.nct_id in cited)
    ]


def overlap_assumptions(spec: DimensionSpec, overlap: bool, total: int) -> list[Notice]:
    """Explain why bucket counts may exceed the number of trials."""
    if not overlap:
        return []
    return [
        Notice.of(
            NoticeCode.BUCKETS_OVERLAP,
            f"A trial counts once in each {spec.label} it lists, so bucket counts can add up "
            f"to more than the {total} trials.",
        )
    ]


def bucket_warnings(
    spec: DimensionSpec, buckets: list[Bucket], total: int, as_of: date
) -> list[Notice]:
    """Report Unknown, estimated, in-progress and future buckets (invariant 3)."""
    warnings: list[Notice] = []
    unknown = sum(b.count for b in buckets if b.is_unknown)
    if unknown and spec.unknown_warning is not None:
        text = spec.unknown_warning.format(unknown=unknown, total=total)
        warnings.append(Notice.of(NoticeCode.UNKNOWN_BUCKET, text))
    elif unknown:
        warnings.append(
            Notice.of(
                NoticeCode.UNKNOWN_BUCKET,
                f"{unknown} of {total} trials have no {spec.label}; "
                f"they are counted in the '{UNKNOWN_BUCKET_KEY}' bucket.",
            )
        )
    # None (not determined on the count route) is stated as an assumption, not warned on.
    estimated = sum(b.estimated_count or 0 for b in buckets)
    if estimated:
        code = (
            NoticeCode.ESTIMATED_ENROLLMENT
            if spec.estimate_kind == EstimateKind.ENROLLMENT
            else NoticeCode.ESTIMATED_DATES
        )
        warnings.append(
            Notice.of(
                code,
                f"{plural(estimated, 'trial')} {verb(estimated, 'is', 'are')} counted using "
                f"an estimated {spec.label}.",
            )
        )
    untyped = sum(b.unknown_date_type_count or 0 for b in buckets)
    if untyped:
        warnings.append(
            Notice.of(
                NoticeCode.UNTYPED_DATES,
                f"{plural(untyped, 'trial')} {verb(untyped, 'has', 'have')} a {spec.label} "
                "with no ACTUAL/ESTIMATED type (legacy records); they are counted but marked "
                "neither actual nor estimated.",
            )
        )
    warnings.extend(
        Notice.of(
            NoticeCode.INCOMPLETE_PERIOD,
            f"Bucket '{b.key}' is still in progress as of {as_of.isoformat()}; its count may grow.",
        )
        for b in buckets
        if b.incomplete
    )
    future = [b for b in buckets if b.is_future]
    if future:
        in_future = sum(b.count for b in future)
        warnings.append(
            Notice.of(
                NoticeCode.FUTURE_PERIODS,
                f"{plural(in_future, 'trial')} {verb(in_future, 'has', 'have')} "
                f"{spec.label}s in the future (estimated); buckets {future[0].key}… are "
                "projections, not activity.",
            )
        )
    return warnings
