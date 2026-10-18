"""Group several cohorts on one shared bucket axis, optionally limited to the top N."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from ctviz.aggregate.group import DEFAULT_CITATION_CAP, group_by
from ctviz.aggregate.registry import DIMENSIONS, DimensionSpec, distinct_values
from ctviz.schemas.aggregate import Bucket, CohortGroup, GroupedCohorts, GroupResult
from ctviz.schemas.plan import Dimension
from ctviz.schemas.response import TopNSummary
from ctviz.schemas.trial import TrialRecord


def group_cohorts(
    cohorts: Sequence[tuple[str, Sequence[TrialRecord]]],
    dimension: Dimension,
    *,
    top_n: int | None = None,
    citation_cap: int = DEFAULT_CITATION_CAP,
    as_of: date | None = None,
) -> GroupedCohorts:
    """Group each (label, records) cohort by `dimension` on a shared key order.

    Rules:
    - Each cohort is grouped independently with `group_by`; counts are never mixed.
    - `key_order` is the dimension's own bucket order over the union of all cohorts'
      trials (a trial in several cohorts counts once), Unknown last.
    - `top_n` keeps the N known keys with the most trials in that union (ties follow
      `key_order`) and keeps the Unknown bucket. Kept buckets stay in `key_order`, their
      counts unchanged. No "Other" bucket is made: for overlapping dimensions its count
      would double-count trials. Each cohort gets a warning, and `top_n` in the result
      lists the omitted keys and, per cohort, the trials visible only in omitted buckets.
    """
    as_of = as_of or date.today()
    grouped = [
        (label, records, group_by(records, dimension, citation_cap=citation_cap, as_of=as_of))
        for label, records in cohorts
    ]
    combined = group_by(
        distinct_records(r for _, records in cohorts for r in records),
        dimension,
        citation_cap=0,
        as_of=as_of,
    )
    key_order = [bucket.key for bucket in combined.buckets]
    if top_n is None:
        return GroupedCohorts(
            dimension=dimension,
            cohorts=[CohortGroup(label=label, result=result) for label, _, result in grouped],
            key_order=key_order,
        )

    spec = DIMENSIONS[dimension]
    omitted = omitted_keys(combined.buckets, top_n)
    trials_omitted = {
        label: count_hidden_trials(records, spec, set(omitted)) for label, records, _ in grouped
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
    )


def distinct_records(records: Iterable[TrialRecord]) -> list[TrialRecord]:
    """First occurrence of each NCT ID, so shared trials count once in the union."""
    unique: dict[str, TrialRecord] = {}
    for record in records:
        unique.setdefault(record.nct_id, record)
    return list(unique.values())


def omitted_keys(ordered_buckets: list[Bucket], top_n: int) -> list[str]:
    """Known keys outside the top N by count, in display order; Unknown is never omitted."""
    known = [(index, b) for index, b in enumerate(ordered_buckets) if not b.is_unknown]
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
