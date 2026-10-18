"""Heavy cohort overlap and subsets are warned on, with the likely cause."""

from __future__ import annotations

from ctviz.schemas.aggregate import CohortGroup, GroupedCohorts, GroupResult
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import (
    CohortPairOverlap,
    CohortsOverlap,
    CohortStats,
    Meta,
    NoticeCode,
    NoticeSeverity,
)
from ctviz.spec import build_response


def stats(label: str, count: int) -> CohortStats:
    return CohortStats(
        label=label,
        total_matched=count,
        records_retrieved=count,
        records_skipped=0,
        truncated=False,
        coverage=1.0,
        reconciled=True,
    )


def meta_for(cohorts: list[tuple[str, Filters, int]], shared: int) -> Meta:
    plan = QueryPlan(
        cohorts=[Cohort(label=label, filters=filters) for label, filters, _ in cohorts],
        dimension=Dimension.SPONSOR_CLASS,
    )
    labels = [label for label, _, _ in cohorts]
    result = GroupedCohorts(
        dimension=Dimension.SPONSOR_CLASS,
        cohorts=[
            CohortGroup(
                label=label,
                result=GroupResult(
                    dimension=Dimension.SPONSOR_CLASS, buckets=[], buckets_overlap=False
                ),
            )
            for label in labels
        ],
        key_order=[],
        cohorts_overlap=CohortsOverlap(
            pairs=[CohortPairOverlap(cohorts=labels, shared_trials=shared)], total_shared=shared
        ),
    )
    return build_response(
        plan,
        result,
        question="q",
        cohort_stats=[stats(label, count) for label, _, count in cohorts],
        search_semantics={},
        data_timestamp=None,
        timing_ms={},
    ).meta


def test_nsclc_vs_sclc_overlap_is_a_warning_with_the_term_search_cause() -> None:
    meta = meta_for(
        [
            ("NSCLC", Filters(condition="NSCLC"), 8800),
            ("small cell lung cancer", Filters(condition="small cell lung cancer"), 9727),
        ],
        shared=8352,
    )
    (warning,) = [w for w in meta.warnings if w.code == NoticeCode.COHORTS_OVERLAP]
    assert warning.severity == NoticeSeverity.WARNING
    assert "8352 of the 8800 NSCLC studies (95%)" in warning.message
    assert "'small cell lung cancer' inside 'non-small cell lung cancer'" in warning.message
    assert "not a comparison of separate groups" in warning.message


def test_substring_condition_names_the_exact_terms() -> None:
    meta = meta_for(
        [
            ("non-small cell lung cancer", Filters(condition="non-small cell lung cancer"), 8601),
            ("small cell lung cancer", Filters(condition="small cell lung cancer"), 9727),
        ],
        shared=8400,
    )
    (warning,) = [w for w in meta.warnings if w.code == NoticeCode.COHORTS_OVERLAP]
    assert (
        "the term search for 'small cell lung cancer' also matches "
        "'non-small cell lung cancer'" in warning.message
    )


def test_trastuzumab_vs_t_dxd_is_a_subset() -> None:
    meta = meta_for(
        [
            ("trastuzumab", Filters(drug="trastuzumab"), 1321),
            ("T-DXd", Filters(drug="T-DXd"), 45),
        ],
        shared=45,
    )
    (warning,) = [w for w in meta.warnings if w.code == NoticeCode.COHORT_SUBSET]
    assert warning.severity == NoticeSeverity.WARNING
    assert warning.message.startswith("T-DXd is a subset of trastuzumab: all 45 T-DXd studies")
    assert "drug names match by 'contains'" in warning.message
    assert not [w for w in meta.warnings if w.code == NoticeCode.COHORTS_OVERLAP]


def test_small_overlap_stays_an_assumption() -> None:
    meta = meta_for(
        [
            ("pembrolizumab", Filters(drug="pembrolizumab"), 2642),
            ("nivolumab", Filters(drug="nivolumab"), 1760),
        ],
        shared=173,
    )
    assert not [
        w for w in meta.warnings if w.code in (NoticeCode.COHORTS_OVERLAP, NoticeCode.COHORT_SUBSET)
    ]
    (note,) = [a for a in meta.assumptions if a.code == NoticeCode.COHORTS_OVERLAP]
    assert note.severity == NoticeSeverity.INFO
