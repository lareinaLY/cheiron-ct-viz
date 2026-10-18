"""Buckets counted by the API (the count-per-bucket route) -> a cited GroupResult.

The counts are the API's exact totalCount per bucket filter. The sample records each
bucket request returned are not counted: they supply the citations, the label and the
sort rank, through the same registry extractors the fetch route uses, so both routes
produce the same keys, labels and order.
"""

from __future__ import annotations

from datetime import date

from ctviz.aggregate.group import (
    DEFAULT_CITATION_CAP,
    BucketTally,
    bucket_warnings,
    is_future_period,
    sort_tallies,
    to_bucket,
    unknown_value,
)
from ctviz.aggregate.registry import (
    DIMENSIONS,
    DimensionKind,
    DimensionSpec,
    DimensionValue,
    distinct_values,
)
from ctviz.schemas.aggregate import Bucket, CountedBucket, CountedCohort, GroupResult
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import TrialRecord


def group_counted(
    cohort: CountedCohort,
    dimension: Dimension,
    *,
    citation_cap: int = DEFAULT_CITATION_CAP,
    as_of: date | None = None,
) -> GroupResult:
    """Build the buckets of an API-counted cohort.

    Rules:
    - A bucket's count is the API's count; buckets counted 0 are not shown (the fetch
      route never shows an empty bucket either).
    - Citations are the bucket's sample records (capped at `citation_cap`), and
      `citation_count_total` is the bucket's count: every countable dimension cites one
      value per trial, so the bucket has exactly that many citations at the source.
    - Temporal buckets keep their `incomplete` and `is_future` flags, but how many of
      their trials use an estimated or untyped date is not determined: those counts are
      None (missing is not zero), and an assumption says so.
    Raises ValueError when a sample record does not belong to its bucket, or a counted
    bucket has no readable sample; the pipeline checks `counted_sample_problems` first.
    """
    problems = counted_sample_problems(cohort, dimension)
    if problems:
        raise ValueError("; ".join(problems))
    spec = DIMENSIONS[dimension]
    as_of = as_of or date.today()
    temporal = spec.kind == DimensionKind.TEMPORAL

    tallies = [counted_tally(bucket, spec) for bucket in cohort.buckets if bucket.count > 0]
    current_period = spec.period_key(as_of) if spec.period_key else None
    buckets = [
        counted_bucket(
            tally,
            citation_cap,
            temporal=temporal,
            incomplete=tally.key == current_period,
            is_future=is_future_period(tally, current_period),
        )
        for tally in sort_tallies(tallies, spec.order)
    ]
    assumptions = list(spec.assumptions)
    if temporal:
        assumptions.append(
            f"Each {spec.label} bucket was counted by the API without reading its trials, so "
            f"how many of them use an estimated or untyped (legacy) {spec.label} is not "
            "determined: estimated_count and unknown_date_type_count are null, not zero."
        )
    return GroupResult(
        dimension=dimension,
        buckets=buckets,
        buckets_overlap=False,
        estimated_count=None if temporal else 0,
        unknown_date_type_count=None,
        assumptions=assumptions,
        warnings=bucket_warnings(spec, buckets, cohort.total_count, as_of),
    )


def counted_sample_problems(cohort: CountedCohort, dimension: Dimension) -> list[str]:
    """Reasons the per-bucket counts cannot be trusted, judged from the sample records.

    A sample record whose own value puts it in another bucket means the bucket's API
    filter does not select what our normalizer calls that bucket, so the count would be
    wrong. A counted bucket without a readable sample can be neither cited nor labeled.
    """
    spec = DIMENSIONS[dimension]
    problems: list[str] = []
    for bucket in cohort.buckets:
        if bucket.count > 0 and not bucket.records:
            problems.append(
                f"bucket '{bucket.key}' was counted {bucket.count} but returned no readable "
                "sample record to cite"
            )
        for record in bucket.records:
            keys = [value.key for value in record_values(record, spec)]
            if bucket.key not in keys:
                problems.append(
                    f"{record.nct_id} was returned for bucket '{bucket.key}' but its "
                    f"{spec.label} puts it in {', '.join(repr(key) for key in keys)}"
                )
    return problems


def record_values(record: TrialRecord, spec: DimensionSpec) -> list[DimensionValue]:
    """The record's values for the dimension, or its Unknown entry when it has none."""
    return distinct_values(record, spec) or [unknown_value(record, spec)]


def counted_tally(bucket: CountedBucket, spec: DimensionSpec) -> BucketTally:
    """A tally holding the API count and the sample records' citations for the bucket."""
    values = [
        value
        for record in bucket.records
        for value in record_values(record, spec)
        if value.key == bucket.key
    ]
    tally = BucketTally.first_of(values[0])
    tally.count = bucket.count
    tally.citations = [citation for value in values for citation in value.citations]
    return tally


def counted_bucket(
    tally: BucketTally, citation_cap: int, *, temporal: bool, incomplete: bool, is_future: bool
) -> Bucket:
    """Freeze a counted tally: the true citation total is the count, not the sample size."""
    bucket = to_bucket(tally, citation_cap, incomplete=incomplete, is_future=is_future)
    undetermined = None if temporal else 0
    return bucket.model_copy(
        update={
            "citation_count_total": tally.count,
            "estimated_count": undetermined,
            "unknown_date_type_count": undetermined,
        }
    )
