"""Group several cohorts on one shared bucket axis, optionally limited to the top N."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from itertools import combinations

from ctviz.aggregate.counted import group_counted
from ctviz.aggregate.group import (
    DEFAULT_CITATION_CAP,
    BucketTally,
    group_by,
    sort_tallies,
    to_bucket,
)
from ctviz.aggregate.registry import DIMENSIONS, DimensionSpec, distinct_values
from ctviz.schemas.aggregate import (
    Bucket,
    CohortGroup,
    CountedCohort,
    GroupedCohorts,
    GroupResult,
)
from ctviz.schemas.plan import Dimension
from ctviz.schemas.response import CohortPairOverlap, CohortsOverlap, TopNSummary
from ctviz.schemas.trial import TrialRecord

# A cohort's trials as fetched (fetch route), or its buckets as counted by the API.
type CohortSource = Sequence[TrialRecord] | CountedCohort


def group_cohorts(
    cohorts: Sequence[tuple[str, CohortSource]],
    dimension: Dimension,
    *,
    top_n: int | None = None,
    citation_cap: int = DEFAULT_CITATION_CAP,
    as_of: date | None = None,
) -> GroupedCohorts:
    """Group each (label, records or counted buckets) cohort by `dimension` on a shared key order.

    Rules:
    - Each cohort is grouped independently (`group_by` for records, `group_counted` for
      API-counted buckets); counts are never mixed.
    - `key_order` is the dimension's own bucket order over the union of all cohorts'
      trials (a trial in several cohorts counts once), Unknown last. When any cohort was
      counted by the API its trials are not known individually, so the order is taken
      over the cohorts' summed bucket counts instead (only by-count order and the top N
      ranking can differ, and only for trials shared between cohorts).
    - `top_n` keeps the N known keys with the most trials in that union (ties follow
      `key_order`) and keeps the Unknown and Unrecognized buckets. Kept buckets stay in
      `key_order`, their counts unchanged. No "Other" bucket is made: for overlapping
      dimensions its count would double-count trials. Each cohort gets a warning, and
      `top_n` in the result lists the omitted keys and, per cohort, the trials visible
      only in omitted buckets (for a counted cohort: the summed counts of its omitted
      buckets, exact because every API-countable dimension is single-valued).
    - `cohorts_overlap` reports trials found in more than one cohort (see `cohorts_overlap`).
    """
    as_of = as_of or date.today()
    spec = DIMENSIONS[dimension]
    grouped = [
        (label, source, group_source(source, dimension, citation_cap, as_of))
        for label, source in cohorts
    ]
    combined = combined_buckets(cohorts, [result for _, _, result in grouped], spec, as_of)
    key_order = [bucket.key for bucket in combined]
    overlap = cohorts_overlap(cohorts)
    if top_n is None:
        return GroupedCohorts(
            dimension=dimension,
            cohorts=[CohortGroup(label=label, result=result) for label, _, result in grouped],
            key_order=key_order,
            cohorts_overlap=overlap,
        )

    omitted = omitted_keys(combined, top_n)
    trials_omitted = {
        label: hidden_trials(source, result, spec, set(omitted))
        for label, source, result in grouped
    }
    return GroupedCohorts(
        dimension=dimension,
        cohorts=[
            CohortGroup(
                label=label,
                result=drop_buckets(result, spec, top_n, omitted, trials_omitted[label]),
            )
            for label, _, result in grouped
        ],
        key_order=[key for key in key_order if key not in omitted],
        top_n=TopNSummary(top_n=top_n, omitted_keys=omitted, trials_omitted=trials_omitted),
        cohorts_overlap=overlap,
    )


def cohorts_overlap(cohorts: Sequence[tuple[str, CohortSource]]) -> CohortsOverlap | None:
    """NCT IDs shared by each pair of cohorts, and by any two cohorts overall.

    None for a single cohort (nothing to overlap) and when any cohort was counted by the
    API: its trials are known only through per-bucket samples, so shared trials cannot be
    found.
    """
    if len(cohorts) < 2 or any(isinstance(source, CountedCohort) for _, source in cohorts):
        return None
    ids = {
        label: {record.nct_id for record in source}
        for label, source in cohorts
        if not isinstance(source, CountedCohort)
    }
    pairs = [
        CohortPairOverlap(cohorts=[a, b], shared_trials=len(ids[a] & ids[b]))
        for a, b in combinations(ids, 2)
    ]
    cohort_count = Counter(nct_id for nct_ids in ids.values() for nct_id in nct_ids)
    total_shared = sum(1 for count in cohort_count.values() if count > 1)
    return CohortsOverlap(pairs=pairs, total_shared=total_shared)


def group_source(
    source: CohortSource, dimension: Dimension, citation_cap: int, as_of: date
) -> GroupResult:
    """Group one cohort by the route its data came from."""
    if isinstance(source, CountedCohort):
        return group_counted(source, dimension, citation_cap=citation_cap, as_of=as_of)
    return group_by(source, dimension, citation_cap=citation_cap, as_of=as_of)


def combined_buckets(
    cohorts: Sequence[tuple[str, CohortSource]],
    results: Sequence[GroupResult],
    spec: DimensionSpec,
    as_of: date,
) -> list[Bucket]:
    """Buckets over all cohorts in display order, used for the key order and the top N.

    With records only, they are the union of distinct trials. With any counted cohort,
    per-cohort buckets are merged by key with their counts summed.
    """
    sources = [source for _, source in cohorts]
    if not any(isinstance(source, CountedCohort) for source in sources):
        records = [r for source in sources if not isinstance(source, CountedCohort) for r in source]
        return group_by(
            distinct_records(records), results[0].dimension, citation_cap=0, as_of=as_of
        ).buckets
    tallies: dict[str, BucketTally] = {}
    for result in results:
        for bucket in result.buckets:
            tally = tallies.setdefault(
                bucket.key,
                BucketTally(
                    bucket.key, bucket.label, bucket.is_unknown, bucket.is_unrecognized, bucket.rank
                ),
            )
            tally.count += bucket.count
    return [
        to_bucket(tally, 0, incomplete=False, is_future=False)
        for tally in sort_tallies(list(tallies.values()), spec.order)
    ]


def hidden_trials(
    source: CohortSource, result: GroupResult, spec: DimensionSpec, omitted: set[str]
) -> int:
    """Trials of one cohort that appear only in omitted buckets."""
    if isinstance(source, CountedCohort):
        return sum(bucket.count for bucket in result.buckets if bucket.key in omitted)
    return count_hidden_trials(source, spec, omitted)


def distinct_records(records: Iterable[TrialRecord]) -> list[TrialRecord]:
    """First occurrence of each NCT ID, so shared trials count once in the union."""
    unique: dict[str, TrialRecord] = {}
    for record in records:
        unique.setdefault(record.nct_id, record)
    return list(unique.values())


def omitted_keys(ordered_buckets: list[Bucket], top_n: int) -> list[str]:
    """Known keys outside the top N by count, in display order.

    Unknown and Unrecognized are never omitted: they describe data quality, not a value
    competing for the top N.
    """
    known = [
        (index, b)
        for index, b in enumerate(ordered_buckets)
        if not (b.is_unknown or b.is_unrecognized)
    ]
    ranked = sorted(known, key=lambda item: (-item[1].count, item[0]))
    kept = {bucket.key for _, bucket in ranked[:top_n]}
    return [bucket.key for _, bucket in known if bucket.key not in kept]


def count_hidden_trials(
    records: Sequence[TrialRecord], spec: DimensionSpec, omitted: set[str]
) -> int:
    """Trials whose every value is omitted, so they appear nowhere in the chart."""
    hidden = 0
    for record in records:
        keys = {value.key for value in distinct_values(record, spec)}
        hidden += int(bool(keys) and keys <= omitted)
    return hidden


def drop_buckets(
    result: GroupResult, spec: DimensionSpec, top_n: int, omitted: list[str], hidden: int
) -> GroupResult:
    """Remove omitted buckets and state what was removed (invariant 3: nothing is silent)."""
    if not omitted:
        return result
    warning = (
        f"Showing the top {top_n} {spec.label} values by trial count; {len(omitted)} "
        f"{spec.label} values omitted; {hidden} trials appear only in omitted values "
        "and are not shown."
    )
    return result.model_copy(
        update={
            "buckets": [b for b in result.buckets if b.key not in set(omitted)],
            "warnings": [*result.warnings, warning],
        }
    )
