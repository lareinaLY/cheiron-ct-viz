"""Top-N and bottom-N: the 'Other' bucket for exclusive dimensions, and ties at the cutoff."""

from __future__ import annotations

from datetime import date

from ctviz.aggregate import UNKNOWN_BUCKET_KEY, group_cohorts
from ctviz.aggregate.registry import DIMENSIONS
from ctviz.schemas.aggregate import CountedBucket, CountedCohort, GroupedCohorts
from ctviz.schemas.plan import Dimension
from ctviz.schemas.response import NoticeCode
from ctviz.schemas.trial import AgencyClass, Sponsor, TrialRecord
from tests.factories import country, make_record

AS_OF = date(2026, 10, 17)
OTHER = "Other"


def sponsored(nct_id: str, name: str | None) -> TrialRecord:
    if name is None:
        return make_record(nct_id)
    return make_record(nct_id, lead_sponsor=Sponsor(name=name, agency_class=AgencyClass.OTHER))


def sponsor_records(counts: dict[str, int]) -> list[TrialRecord]:
    return [
        sponsored(f"NCT{name}{i}", name) for name, count in counts.items() for i in range(count)
    ]


def located(nct_id: str, *names: str) -> TrialRecord:
    return make_record(
        nct_id, country_locations=[country(n, location_index=i) for i, n in enumerate(names)]
    )


def keys(result: GroupedCohorts, cohort_index: int = 0) -> list[str]:
    return [b.key for b in result.cohorts[cohort_index].result.buckets]


class TestOtherBucket:
    def test_registry_marks_which_dimensions_get_an_other_bucket(self) -> None:
        exclusive = {d for d, spec in DIMENSIONS.items() if spec.other_bucket}
        assert exclusive == {
            Dimension.PHASE,
            Dimension.OVERALL_STATUS,
            Dimension.SPONSOR_CLASS,
            Dimension.LEAD_SPONSOR,
            Dimension.ENROLLMENT_BUCKET,
        }

    def test_exclusive_dimension_gets_an_exact_other_bucket(self) -> None:
        records = [*sponsor_records({"A": 3, "B": 2, "C": 1, "D": 1}), sponsored("NCTX", None)]
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, top_n=2, as_of=AS_OF)
        assert keys(result) == ["A", "B", OTHER, UNKNOWN_BUCKET_KEY]
        other = result.cohorts[0].result.buckets[2]
        assert other.is_other is True
        assert other.label == "Other (2 values)"
        assert other.count == 2
        assert other.cited_trials_total == 2
        assert {c.nct_id for c in other.citations} == {"NCTC0", "NCTD0"}
        assert result.key_order == ["A", "B", OTHER, UNKNOWN_BUCKET_KEY]
        assert result.top_n is not None
        assert result.top_n.trials_omitted == {"a": 2}

    def test_other_citations_are_capped(self) -> None:
        records = sponsor_records({"A": 30, **{f"S{i}": 1 for i in range(25)}})
        result = group_cohorts(
            [("a", records)], Dimension.LEAD_SPONSOR, top_n=1, citation_cap=20, as_of=AS_OF
        )
        other = next(b for b in result.cohorts[0].result.buckets if b.is_other)
        assert other.count == 25
        assert len(other.citations) == 20
        assert other.citation_count_total == 25

    def test_other_bucket_is_stated_as_an_assumption(self) -> None:
        records = sponsor_records({"A": 3, "B": 2, "C": 1})
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, top_n=2, as_of=AS_OF)
        (note,) = [a for a in result.assumptions if a.code == NoticeCode.OTHER_BUCKET]
        assert "Other (1 value)" in note.message

    def test_each_cohort_gets_its_own_other_count(self) -> None:
        cohort_a = sponsor_records({"A": 3, "B": 1, "C": 1})
        cohort_b = [sponsored("NCTb1", "A"), sponsored("NCTb2", "C"), sponsored("NCTb3", "D")]
        result = group_cohorts(
            [("a", cohort_a), ("b", cohort_b)], Dimension.LEAD_SPONSOR, top_n=1, as_of=AS_OF
        )
        others = [next(b for b in cohort.result.buckets if b.is_other) for cohort in result.cohorts]
        assert [b.count for b in others] == [2, 2]
        assert {b.label for b in others} == {"Other (3 values)"}

    def test_no_other_bucket_when_nothing_is_omitted(self) -> None:
        records = sponsor_records({"A": 3, "B": 2})
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, top_n=5, as_of=AS_OF)
        assert OTHER not in keys(result)

    def test_overlapping_dimension_has_no_other_bucket_and_says_why(self) -> None:
        records = [located("NCT1", "US", "France"), located("NCT2", "US"), located("NCT3", "Peru")]
        result = group_cohorts([("a", records)], Dimension.COUNTRY, top_n=1, as_of=AS_OF)
        assert keys(result) == ["US"]
        (note,) = [a for a in result.assumptions if a.code == NoticeCode.OTHER_BUCKET]
        assert "double-count" in note.message
        assert "trials_omitted" in note.message

    def test_counted_cohort_other_is_the_sum_of_omitted_counts(self) -> None:
        def bucket(key: str, count: int) -> CountedBucket:
            sponsor = Sponsor(name="X", agency_class=AgencyClass(key))
            return CountedBucket(
                key=key, count=count, records=[make_record(f"NCT{key}", lead_sponsor=sponsor)]
            )

        counted = CountedCohort(
            total_count=600,
            buckets=[bucket("INDUSTRY", 400), bucket("OTHER", 150), bucket("NIH", 50)],
        )
        result = group_cohorts([("a", counted)], Dimension.SPONSOR_CLASS, top_n=1, as_of=AS_OF)
        other = next(b for b in result.cohorts[0].result.buckets if b.is_other)
        assert other.count == 200
        assert other.citation_count_total == 200
        assert other.cited_trials_total == 200


class TestTies:
    def test_omitted_values_sharing_the_cutoff_count_are_reported(self) -> None:
        records = [
            located("NCT1", "A"),
            located("NCT2", "A"),
            located("NCT3", "B"),
            located("NCT4", "C"),
            located("NCT5", "D"),
        ]
        result = group_cohorts([("a", records)], Dimension.COUNTRY, top_n=2, as_of=AS_OF)
        assert result.top_n is not None
        assert result.top_n.omitted_ties == 2
        (warning,) = [w for w in result.warnings if w.code == NoticeCode.TOPN_TIES]
        assert warning.message == ("2 more values share the cutoff count of 1 and are not shown.")

    def test_bottom_n_ties_are_reported_in_the_singular(self) -> None:
        records = sponsor_records({"A": 3, "B": 2, "C": 1, "D": 1, "E": 1})
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, bottom_n=2, as_of=AS_OF)
        assert result.top_n is not None
        assert result.top_n.omitted_ties == 1
        (warning,) = [w for w in result.warnings if w.code == NoticeCode.TOPN_TIES]
        assert warning.message == "1 more value shares the cutoff count of 1 and is not shown."

    def test_no_tie_warning_when_the_cutoff_is_clean(self) -> None:
        records = sponsor_records({"A": 3, "B": 2, "C": 1})
        result = group_cohorts([("a", records)], Dimension.LEAD_SPONSOR, top_n=2, as_of=AS_OF)
        assert result.top_n is not None
        assert result.top_n.omitted_ties == 0
        assert not [w for w in result.warnings if w.code == NoticeCode.TOPN_TIES]
