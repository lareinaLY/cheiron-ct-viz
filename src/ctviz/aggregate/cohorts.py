"""Group several cohorts on one shared bucket axis, optionally limited to the top or bottom N."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from itertools import combinations

from ctviz.aggregate.counted import group_counted
from ctviz.aggregate.group import (
    DEFAULT_CITATION_CAP,
    BucketTally,
    cap_citations,
    group_by,
    sort_tallies,
    to_bucket,
)
from ctviz.aggregate.registry import DIMENSIONS, DimensionKind, DimensionSpec, distinct_values
from ctviz.schemas.aggregate import (
    Bucket,
    CohortGroup,
    CountedCohort,
    GroupedCohorts,
    GroupResult,
)
from ctviz.schemas.plan import Dimension, RankingDirection
from ctviz.schemas.response import (
    CohortPairOverlap,
    CohortsOverlap,
    Notice,
    NoticeCode,
    TopNSummary,
)
from ctviz.schemas.trial import TrialRecord
from ctviz.text import plural, verb

OTHER_BUCKET_KEY = "Other"

# A cohort's trials as fetched (fetch route), or its buckets as counted by the API.
type CohortSource = Sequence[TrialRecord] | CountedCohort


def group_cohorts(
    cohorts: Sequence[tuple[str, CohortSource]],
    dimension: Dimension,
    *,
    top_n: int | None = None,
    bottom_n: int | None = None,
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
      `key_order`, their counts unchanged. Each cohort gets a warning, and `top_n` in the
      result lists the omitted keys and, per cohort, the trials visible only in omitted
      buckets (for a counted cohort: the summed counts of its omitted buckets, exact
      because every API-countable dimension is single-valued). How many omitted values
      tie with the last kept one is counted and warned on.
    - On a dimension with `other_bucket` (single-valued, not temporal), each cohort with
      omitted trials gets an "Other (N values)" bucket (`is_other`) after the kept
      values: the exact number of its trials in the omitted values, citations capped as
      usual. Overlapping dimensions get none (its count would double-count trials); an
      assumption says which applies.
    - `bottom_n` is the same with the N known keys with the fewest trials (ties follow
      `key_order`). Values with no matching trials have no bucket, so they cannot be among
      the bottom N; the warning says so even when nothing is omitted. `top_n` in the
      result carries `direction` "fewest".
    - `cohorts_overlap` reports trials found in more than one cohort (see `cohorts_overlap`).
    """
    if top_n is not None and bottom_n is not None:
        raise ValueError(f"top_n and bottom_n are exclusive; got {top_n} and {bottom_n}.")
    as_of = as_of or date.today()
    spec = DIMENSIONS[dimension]
    grouped = [
        (label, source, group_source(source, dimension, citation_cap, as_of))
        for label, source in cohorts
    ]
    combined = combined_buckets(cohorts, [result for _, _, result in grouped], spec, as_of)
    key_order = [bucket.key for bucket in combined]
    overlap = cohorts_overlap(cohorts)
    if top_n is None and bottom_n is None:
        return GroupedCohorts(
            dimension=dimension,
            cohorts=[CohortGroup(label=label, result=result) for label, _, result in grouped],
            key_order=key_order,
            cohorts_overlap=overlap,
        )

    direction = RankingDirection.MOST if top_n is not None else RankingDirection.FEWEST
    keep = top_n if top_n is not None else bottom_n
    if keep is None:
        raise ValueError("A ranked grouping needs top_n or bottom_n.")
    kept, omitted_buckets = ranking(combined, keep, direction)
    omitted = [bucket.key for bucket in omitted_buckets]
    cutoff = kept[-1].count if kept else None
    ties = sum(1 for bucket in omitted_buckets if bucket.count == cutoff)
    trials_omitted = {
        label: hidden_trials(source, result, spec, set(omitted))
        for label, source, result in grouped
    }
    results = [
        drop_buckets(result, spec, keep, direction, omitted, trials_omitted[label])
        for label, _, result in grouped
    ]
    if spec.other_bucket and omitted:
        results = [
            with_other_bucket(dropped, full, omitted, citation_cap)
            for dropped, (_, _, full) in zip(results, grouped, strict=True)
        ]
    has_other = any(bucket.is_other for result in results for bucket in result.buckets)
    return GroupedCohorts(
        dimension=dimension,
        cohorts=[
            CohortGroup(label=label, result=result)
            for (label, _, _), result in zip(grouped, results, strict=True)
        ],
        key_order=ranked_key_order(combined, set(omitted), has_other),
        top_n=TopNSummary(
            top_n=keep,
            direction=direction,
            omitted_keys=omitted,
            trials_omitted=trials_omitted,
            cutoff_count=cutoff,
            omitted_ties=ties,
        ),
        cohorts_overlap=overlap,
        assumptions=other_bucket_assumptions(spec, omitted, has_other),
        warnings=tie_warnings(ties, cutoff),
    )


def ranked_key_order(combined: list[Bucket], omitted: set[str], has_other: bool) -> list[str]:
    """Kept known keys in display order, then Other, then Unrecognized and Unknown."""
    flagged = [b.key for b in combined if b.is_unknown or b.is_unrecognized]
    known = [b.key for b in combined if b.key not in omitted and b.key not in flagged]
    return [*known, *([OTHER_BUCKET_KEY] if has_other else []), *flagged]


def other_label(omitted: Sequence[str]) -> str:
    """'Other (36 values)': the same text in every cohort, so the bars share one axis slot."""
    return f"Other ({plural(len(omitted), 'value')})"


def with_other_bucket(
    dropped: GroupResult, full: GroupResult, omitted: list[str], citation_cap: int
) -> GroupResult:
    """Add the cohort's Other bucket (its trials in omitted values) before Unrecognized/Unknown.

    Exact because the dimension is single-valued: each trial is in one omitted bucket at
    most, so the counts add up to distinct trials. No bucket when the cohort has none.
    """
    omitted_set = set(omitted)
    parts = [bucket for bucket in full.buckets if bucket.key in omitted_set]
    count = sum(bucket.count for bucket in parts)
    if count == 0:
        return dropped
    estimates = [bucket.estimated_count for bucket in parts]
    other = Bucket(
        key=OTHER_BUCKET_KEY,
        label=other_label(omitted),
        count=count,
        estimated_count=(
            None if any(e is None for e in estimates) else sum(e or 0 for e in estimates)
        ),
        unknown_date_type_count=None,
        is_other=True,
        citations=cap_citations(
            [citation for bucket in parts for citation in bucket.citations], citation_cap
        ),
        cited_trials_total=sum(bucket.cited_trials_total for bucket in parts),
        citation_count_total=sum(bucket.citation_count_total for bucket in parts),
    )
    known = [b for b in dropped.buckets if not (b.is_unknown or b.is_unrecognized)]
    flagged = [b for b in dropped.buckets if b.is_unknown or b.is_unrecognized]
    return dropped.model_copy(update={"buckets": [*known, other, *flagged]})


def other_bucket_assumptions(
    spec: DimensionSpec, omitted: list[str], has_other: bool
) -> list[Notice]:
    """Say what the Other bar counts, or why there is none."""
    if not omitted:
        return []
    if has_other:
        text = (
            f"The '{other_label(omitted)}' bar counts the trials in the {len(omitted)} omitted "
            f"{spec.label} values; each trial has one {spec.label}, so the count is exact and "
            "no trial is counted twice."
        )
    elif spec.kind == DimensionKind.TEMPORAL:
        text = (
            f"No 'Other' bar: omitted {spec.label} periods have no single position on a time "
            "axis; meta.top_n.trials_omitted gives the trials that appear only in them."
        )
    else:
        text = (
            f"No 'Other' bar: a trial can count in several {spec.label} values, so summing "
            "the omitted values would double-count trials; meta.top_n.trials_omitted gives "
            "the trials that appear only in omitted values."
        )
    return [Notice.of(NoticeCode.OTHER_BUCKET, text)]


def tie_warnings(ties: int, cutoff: int | None) -> list[Notice]:
    """Report omitted values that tie with the last shown one (left out by display order)."""
    if not ties:
        return []
    return [
        Notice.of(
            NoticeCode.TOPN_TIES,
            f"{ties} more {'value' if ties == 1 else 'values'} {verb(ties, 'shares', 'share')} "
            f"the cutoff count of {cutoff} and {verb(ties, 'is', 'are')} not shown.",
        )
    ]


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
                    bucket.key,
                    bucket.label,
                    bucket.is_unknown,
                    bucket.is_unrecognized,
                    bucket.rank,
                    bucket.is_not_applicable,
                ),
            )
            tally.count += bucket.count
    return [
        to_bucket(tally, 0, spec, incomplete=False, is_future=False)
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


def ranking(
    ordered_buckets: list[Bucket], keep: int, direction: RankingDirection
) -> tuple[list[Bucket], list[Bucket]]:
    """(kept, omitted) known buckets: kept in rank order, omitted in display order.

    Unknown and Unrecognized are in neither: they describe data quality, not a value
    competing for the ranking. Ties follow the display order in both directions.
    """
    known = [
        (index, b)
        for index, b in enumerate(ordered_buckets)
        if not (b.is_unknown or b.is_unrecognized)
    ]
    sign = -1 if direction == RankingDirection.MOST else 1
    ranked = sorted(known, key=lambda item: (sign * item[1].count, item[0]))
    kept = [bucket for _, bucket in ranked[:keep]]
    kept_keys = {bucket.key for bucket in kept}
    return kept, [bucket for _, bucket in known if bucket.key not in kept_keys]


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
    result: GroupResult,
    spec: DimensionSpec,
    keep: int,
    direction: RankingDirection,
    omitted: list[str],
    hidden: int,
) -> GroupResult:
    """Remove omitted buckets and state what was removed (invariant 3: nothing is silent).

    A bottom-N result is always annotated, even with nothing omitted: values with no
    matching trials have no bucket, so "fewest" is only among values with at least one.
    """
    if not omitted and direction == RankingDirection.MOST:
        return result
    end = "top" if direction == RankingDirection.MOST else "bottom"
    warning = (
        f"Showing the {end} {keep} {spec.label} values by trial count; {len(omitted)} "
        f"{spec.label} values omitted; {hidden} trials appear only in omitted values "
        "and are not shown."
    )
    if direction == RankingDirection.FEWEST:
        warning += (
            " Values with no matching trials have no bucket and cannot appear among the "
            "fewest; Unknown and Unrecognized are not ranked and are still shown."
        )
    return result.model_copy(
        update={
            "buckets": [b for b in result.buckets if b.key not in set(omitted)],
            "warnings": [*result.warnings, Notice.of(NoticeCode.TOP_N_APPLIED, warning)],
        }
    )
