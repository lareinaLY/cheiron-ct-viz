"""Aggregation over API-counted buckets (the count-per-bucket route).

Counts come from the API's totalCount per bucket; the sample records only supply
citations, labels and ordering.
"""

from __future__ import annotations

from datetime import date

import pytest

from ctviz.aggregate import (
    UNKNOWN_BUCKET_KEY,
    counted_sample_problems,
    group_by,
    group_cohorts,
    group_counted,
)
from ctviz.schemas.aggregate import Bucket, CountedBucket, CountedCohort, GroupResult
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import OverallStatus, Phase, TrialRecord
from tests.factories import make_record, messages

AS_OF = date(2026, 10, 17)


def bucket(result: GroupResult, key: str) -> Bucket:
    [match] = [b for b in result.buckets if b.key == key]
    return match


def phase_record(nct_id: str, *phases: Phase) -> TrialRecord:
    labels = {
        (Phase.PHASE1,): "Phase 1",
        (Phase.PHASE2,): "Phase 2",
        (Phase.PHASE1, Phase.PHASE2): "Phase 1/Phase 2",
        (Phase.NA,): "Not Applicable",
        (): "Unknown",
    }
    return make_record(nct_id, phases=list(phases), phase_bucket=labels[phases])


def year_record(nct_id: str, year: int, date_type: str = "actual") -> TrialRecord:
    return make_record(
        nct_id,
        start_date=date(year, 1, 1),
        start_date_raw=f"{year}-01-01",
        start_date_precision="day",
        start_date_type=date_type,
    )


def phase_cohort() -> CountedCohort:
    return CountedCohort(
        total_count=5_000,
        buckets=[
            CountedBucket(
                key="Phase 1", count=1_000, records=[phase_record("NCT10", Phase.PHASE1)]
            ),
            CountedBucket(
                key="Phase 1/Phase 2",
                count=500,
                records=[phase_record("NCT12", Phase.PHASE1, Phase.PHASE2)],
            ),
            CountedBucket(
                key="Phase 2",
                count=2_000,
                records=[
                    phase_record("NCT20", Phase.PHASE2),
                    phase_record("NCT21", Phase.PHASE2),
                ],
            ),
            CountedBucket(key="Phase 3", count=0, records=[]),
            CountedBucket(
                key="Not Applicable", count=1_200, records=[phase_record("NCT40", Phase.NA)]
            ),
            CountedBucket(key=UNKNOWN_BUCKET_KEY, count=300, records=[phase_record("NCT50")]),
        ],
    )


class TestGroupCounted:
    def test_counts_are_the_api_counts_not_the_sample_sizes(self) -> None:
        result = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        assert bucket(result, "Phase 2").count == 2_000
        assert bucket(result, "Phase 1").count == 1_000
        assert sum(b.count for b in result.buckets) == 5_000

    def test_citations_are_the_sample_and_the_total_is_the_bucket_count(self) -> None:
        result = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        phase_2 = bucket(result, "Phase 2")
        assert [c.nct_id for c in phase_2.citations] == ["NCT20", "NCT21"]
        assert phase_2.citations[0].field_path == "protocolSection.designModule.phases"
        assert phase_2.citations[0].value == ["PHASE2"]
        assert phase_2.citation_count_total == 2_000

    def test_citations_are_capped(self) -> None:
        result = group_counted(phase_cohort(), Dimension.PHASE, citation_cap=1, as_of=AS_OF)
        assert len(bucket(result, "Phase 2").citations) == 1
        assert bucket(result, "Phase 2").citation_count_total == 2_000

    def test_empty_buckets_are_not_shown(self) -> None:
        result = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        assert "Phase 3" not in {b.key for b in result.buckets}

    def test_buckets_follow_the_dimension_order_with_unknown_last(self) -> None:
        result = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        assert [b.key for b in result.buckets] == [
            "Phase 1",
            "Phase 1/Phase 2",
            "Phase 2",
            "Not Applicable",
            UNKNOWN_BUCKET_KEY,
        ]

    def test_unknown_bucket_is_counted_and_cites_the_absent_field(self) -> None:
        result = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        unknown = bucket(result, UNKNOWN_BUCKET_KEY)
        assert unknown.is_unknown is True
        assert unknown.count == 300
        assert unknown.citations[0].value is None
        # Supersedes "300 of 5000 trials have no phase": Unknown now holds only
        # interventional (or untyped) studies, and the warning says so.
        assert any(
            "300 of 5000 studies are interventional (or have no recognized study type) and "
            "list no phase" in w
            for w in messages(result.warnings)
        )

    def test_same_order_and_labels_as_fetch_route(self) -> None:
        records = [r for b in phase_cohort().buckets for r in b.records]
        fetched = group_by(records, Dimension.PHASE, as_of=AS_OF)
        counted = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        assert [(b.key, b.label) for b in counted.buckets] == [
            (b.key, b.label) for b in fetched.buckets
        ]
        assert messages(counted.assumptions)[: len(messages(fetched.assumptions))] == messages(
            fetched.assumptions
        )

    def test_categorical_estimated_count_is_null(self) -> None:
        # Phase has no estimate concept: null, not 0 (M9).
        result = group_counted(phase_cohort(), Dimension.PHASE, as_of=AS_OF)
        assert result.estimated_count is None
        assert all(b.estimated_count is None for b in result.buckets)

    def test_bucket_without_readable_sample_is_rejected(self) -> None:
        cohort = CountedCohort(
            total_count=10, buckets=[CountedBucket(key="Phase 1", count=10, records=[])]
        )
        with pytest.raises(ValueError, match="Phase 1"):
            group_counted(cohort, Dimension.PHASE, as_of=AS_OF)


class TestTemporal:
    def cohort(self) -> CountedCohort:
        return CountedCohort(
            total_count=60,
            buckets=[
                CountedBucket(key="2025", count=20, records=[year_record("NCT25", 2025)]),
                CountedBucket(key="2026", count=30, records=[year_record("NCT26", 2026)]),
                CountedBucket(
                    key="2027", count=10, records=[year_record("NCT27", 2027, "estimated")]
                ),
            ],
        )

    def test_estimated_and_untyped_counts_are_not_determined(self) -> None:
        # The sample says nothing about how many of a bucket's trials use estimated dates:
        # unknown, not zero (invariant 4).
        result = group_counted(self.cohort(), Dimension.START_YEAR, as_of=AS_OF)
        assert result.estimated_count is None
        assert result.unknown_date_type_count is None
        assert all(b.estimated_count is None for b in result.buckets)
        assert all(b.unknown_date_type_count is None for b in result.buckets)
        assert any("not determined" in a for a in messages(result.assumptions))
        assert not any("estimated start date" in w for w in messages(result.warnings))

    def test_incomplete_and_future_periods_are_flagged(self) -> None:
        result = group_counted(self.cohort(), Dimension.START_YEAR, as_of=AS_OF)
        assert bucket(result, "2026").incomplete is True
        assert bucket(result, "2027").is_future is True
        assert bucket(result, "2025").is_future is False
        assert any(
            "10 trials have start dates in the future" in w for w in messages(result.warnings)
        )


class TestSampleProblems:
    def test_consistent_samples_have_no_problems(self) -> None:
        assert counted_sample_problems(phase_cohort(), Dimension.PHASE) == []

    def test_sample_in_another_bucket_is_a_problem(self) -> None:
        cohort = CountedCohort(
            total_count=10,
            buckets=[
                CountedBucket(
                    key="Phase 2",
                    count=10,
                    records=[phase_record("NCT12", Phase.PHASE1, Phase.PHASE2)],
                )
            ],
        )
        [problem] = counted_sample_problems(cohort, Dimension.PHASE)
        assert "NCT12" in problem
        assert "'Phase 2'" in problem
        assert "Phase 1/Phase 2" in problem

    def test_missing_value_returned_for_a_known_bucket_is_a_problem(self) -> None:
        cohort = CountedCohort(
            total_count=10,
            buckets=[CountedBucket(key="Phase 1", count=10, records=[phase_record("NCT50")])],
        )
        [problem] = counted_sample_problems(cohort, Dimension.PHASE)
        assert "NCT50" in problem

    def test_counted_bucket_without_readable_sample_is_a_problem(self) -> None:
        cohort = CountedCohort(
            total_count=10, buckets=[CountedBucket(key="Phase 1", count=10, records=[])]
        )
        [problem] = counted_sample_problems(cohort, Dimension.PHASE)
        assert "'Phase 1'" in problem
        assert "no readable" in problem


class TestGroupCohortsWithCountedInput:
    def test_single_counted_cohort(self) -> None:
        grouped = group_cohorts([("all", phase_cohort())], Dimension.PHASE, as_of=AS_OF)
        assert grouped.key_order == [
            "Phase 1",
            "Phase 1/Phase 2",
            "Phase 2",
            "Not Applicable",
            UNKNOWN_BUCKET_KEY,
        ]
        assert bucket(grouped.cohorts[0].result, "Phase 2").count == 2_000

    def test_counted_and_fetched_cohorts_share_one_axis(self) -> None:
        fetched = [
            phase_record("NCT90", Phase.PHASE1, Phase.PHASE2),
            phase_record("NCT91", Phase.NA),
        ]
        grouped = group_cohorts(
            [("big", phase_cohort()), ("small", fetched)],
            Dimension.PHASE,
            as_of=AS_OF,
        )
        assert grouped.key_order == [
            "Phase 1",
            "Phase 1/Phase 2",
            "Phase 2",
            "Not Applicable",
            UNKNOWN_BUCKET_KEY,
        ]
        small = grouped.cohorts[1].result
        assert bucket(small, "Phase 1/Phase 2").count == 1

    def test_top_n_on_counted_cohort_counts_hidden_trials_from_bucket_counts(self) -> None:
        grouped = group_cohorts([("all", phase_cohort())], Dimension.PHASE, top_n=2, as_of=AS_OF)
        assert grouped.top_n is not None
        assert grouped.top_n.omitted_keys == ["Phase 1", "Phase 1/Phase 2"]
        # Single-valued dimension: a trial in an omitted bucket appears nowhere else.
        assert grouped.top_n.trials_omitted == {"all": 1_500}
        assert grouped.key_order == ["Phase 2", "Not Applicable", "Other", UNKNOWN_BUCKET_KEY]

    def test_bottom_n_on_counted_cohort_keeps_the_smallest_counted_buckets(self) -> None:
        grouped = group_cohorts([("all", phase_cohort())], Dimension.PHASE, bottom_n=2, as_of=AS_OF)
        assert grouped.top_n is not None
        assert grouped.top_n.omitted_keys == ["Phase 2", "Not Applicable"]
        assert grouped.top_n.trials_omitted == {"all": 3_200}
        assert grouped.key_order == ["Phase 1", "Phase 1/Phase 2", "Other", UNKNOWN_BUCKET_KEY]

    def test_by_count_order_uses_api_counts(self) -> None:
        def status_record(nct_id: str, status: OverallStatus) -> TrialRecord:
            return make_record(nct_id, overall_status=status)

        cohort = CountedCohort(
            total_count=30,
            buckets=[
                CountedBucket(
                    key="RECRUITING",
                    count=10,
                    records=[status_record("NCT1", OverallStatus.RECRUITING)],
                ),
                CountedBucket(
                    key="COMPLETED",
                    count=20,
                    records=[status_record("NCT2", OverallStatus.COMPLETED)],
                ),
            ],
        )
        grouped = group_cohorts([("all", cohort)], Dimension.OVERALL_STATUS, as_of=AS_OF)
        assert grouped.key_order == ["COMPLETED", "RECRUITING"]


class TestCohortsOverlapOnCountRoute:
    def test_overlap_is_not_computed_when_a_cohort_was_counted(self) -> None:
        # Counted cohorts carry only per-bucket samples, not every NCT ID.
        fetched = [phase_record("NCT10", Phase.PHASE1)]
        grouped = group_cohorts(
            [("big", phase_cohort()), ("small", fetched)], Dimension.PHASE, as_of=AS_OF
        )
        assert grouped.cohorts_overlap is None
