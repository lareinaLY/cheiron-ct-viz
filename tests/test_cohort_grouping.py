"""Tests for aggregate.group_cohorts (shared bucket axis across cohorts, top_n)."""

from __future__ import annotations

from datetime import date

from ctviz.aggregate import UNKNOWN_BUCKET_KEY, group_by, group_cohorts
from ctviz.schemas.aggregate import GroupedCohorts
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import Phase, TrialRecord
from tests.factories import country, make_record

AS_OF = date(2026, 10, 17)


def with_countries(nct_id: str, *names: str) -> TrialRecord:
    return make_record(
        nct_id, country_locations=[country(n, location_index=i) for i, n in enumerate(names)]
    )


def with_phase(nct_id: str, phase: Phase | None) -> TrialRecord:
    if phase is None:
        return make_record(nct_id)
    label = {Phase.PHASE1: "Phase 1", Phase.PHASE2: "Phase 2", Phase.PHASE3: "Phase 3"}[phase]
    return make_record(nct_id, phases=[phase], phase_bucket=label)


def with_year(nct_id: str, year: int) -> TrialRecord:
    return make_record(
        nct_id,
        start_date=date(year, 1, 1),
        start_date_raw=f"{year}-01-01",
        start_date_precision="day",
        start_date_type="actual",
    )


def keys(result: GroupedCohorts, cohort_index: int = 0) -> list[str]:
    return [b.key for b in result.cohorts[cohort_index].result.buckets]


class TestSingleCohort:
    def test_matches_group_by(self) -> None:
        records = [with_phase("NCT1", Phase.PHASE2), with_phase("NCT2", None)]
        result = group_cohorts([("a", records)], Dimension.PHASE, as_of=AS_OF)
        assert result.dimension == Dimension.PHASE
        assert result.cohorts[0].label == "a"
        assert result.cohorts[0].result == group_by(records, Dimension.PHASE, as_of=AS_OF)
        assert result.key_order == ["Phase 2", UNKNOWN_BUCKET_KEY]
        assert result.top_n is None


class TestKeyOrder:
    def test_ranked_dimension_order_spans_cohorts(self) -> None:
        cohort_a = [with_phase("NCT1", Phase.PHASE3)]
        cohort_b = [with_phase("NCT2", None), with_phase("NCT3", Phase.PHASE1)]
        result = group_cohorts([("a", cohort_a), ("b", cohort_b)], Dimension.PHASE, as_of=AS_OF)
        assert result.key_order == ["Phase 1", "Phase 3", UNKNOWN_BUCKET_KEY]
        assert keys(result, 0) == ["Phase 3"]
        assert keys(result, 1) == ["Phase 1", UNKNOWN_BUCKET_KEY]

    def test_count_order_counts_a_shared_trial_once(self) -> None:
        cohort_a = [
            with_countries("NCT1", "United States"),
            with_countries("NCT2", "United States"),
        ]
        cohort_b = [
            with_countries("NCT1", "United States"),
            with_countries("NCT3", "France"),
            with_countries("NCT4", "France"),
            with_countries("NCT5", "France"),
        ]
        result = group_cohorts([("a", cohort_a), ("b", cohort_b)], Dimension.COUNTRY, as_of=AS_OF)
        assert result.key_order == ["France", "United States"]


class TestTopN:
    def records(self) -> list[TrialRecord]:
        return [
            with_countries("NCT1", "United States", "Peru"),
            with_countries("NCT2", "United States"),
            with_countries("NCT3", "United States"),
            with_countries("NCT4", "France"),
            with_countries("NCT5", "France"),
            with_countries("NCT6", "Peru"),
            with_countries("NCT7", "Chile"),
            with_countries("NCT8"),
        ]

    def test_keeps_top_n_known_buckets_and_unknown(self) -> None:
        result = group_cohorts([("a", self.records())], Dimension.COUNTRY, top_n=2, as_of=AS_OF)
        assert keys(result) == ["United States", "France", UNKNOWN_BUCKET_KEY]
        assert result.key_order == ["United States", "France", UNKNOWN_BUCKET_KEY]

    def test_reports_omitted_keys_and_trials(self) -> None:
        result = group_cohorts([("a", self.records())], Dimension.COUNTRY, top_n=2, as_of=AS_OF)
        assert result.top_n is not None
        assert result.top_n.top_n == 2
        assert result.top_n.omitted_keys == ["Peru", "Chile"]
        # NCT1 is still visible under United States; NCT6 (Peru) and NCT7 (Chile) are not.
        assert result.top_n.trials_omitted == {"a": 2}

    def test_warning_states_what_was_omitted(self) -> None:
        result = group_cohorts([("a", self.records())], Dimension.COUNTRY, top_n=2, as_of=AS_OF)
        warning = result.cohorts[0].result.warnings[-1]
        assert "top 2" in warning
        assert "2 location country values omitted" in warning
        assert "2 trials" in warning

    def test_kept_bucket_counts_unchanged(self) -> None:
        full = group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
        result = group_cohorts([("a", self.records())], Dimension.COUNTRY, top_n=2, as_of=AS_OF)
        assert result.cohorts[0].result.buckets == [
            b for b in full.buckets if b.key in {"United States", "France", UNKNOWN_BUCKET_KEY}
        ]

    def test_ties_broken_by_dimension_order(self) -> None:
        # Peru and France tie at 2 trials; count order breaks ties by label, so France wins.
        records = [
            with_countries("NCT1", "Peru"),
            with_countries("NCT2", "Peru"),
            with_countries("NCT3", "France"),
            with_countries("NCT4", "France"),
        ]
        result = group_cohorts([("a", records)], Dimension.COUNTRY, top_n=1, as_of=AS_OF)
        assert keys(result) == ["France"]

    def test_temporal_keeps_chronological_order(self) -> None:
        records = [
            with_year("NCT1", 2019),
            with_year("NCT2", 2020),
            with_year("NCT3", 2020),
            with_year("NCT4", 2021),
            with_year("NCT5", 2021),
            with_year("NCT6", 2021),
        ]
        result = group_cohorts([("a", records)], Dimension.START_YEAR, top_n=2, as_of=AS_OF)
        assert keys(result) == ["2020", "2021"]
        assert result.top_n is not None
        assert result.top_n.omitted_keys == ["2019"]

    def test_top_n_across_cohorts_uses_combined_counts(self) -> None:
        cohort_a = [with_countries("NCT1", "Peru"), with_countries("NCT2", "Chile")]
        cohort_b = [with_countries("NCT3", "Chile"), with_countries("NCT4", "Chile")]
        result = group_cohorts(
            [("a", cohort_a), ("b", cohort_b)], Dimension.COUNTRY, top_n=1, as_of=AS_OF
        )
        assert keys(result, 0) == ["Chile"]
        assert keys(result, 1) == ["Chile"]
        assert result.top_n is not None
        assert result.top_n.trials_omitted == {"a": 1, "b": 0}

    def test_top_n_larger_than_bucket_count_omits_nothing(self) -> None:
        result = group_cohorts([("a", self.records())], Dimension.COUNTRY, top_n=50, as_of=AS_OF)
        assert result.top_n is not None
        assert result.top_n.omitted_keys == []
        assert result.top_n.trials_omitted == {"a": 0}
        assert result.cohorts[0].result == group_by(self.records(), Dimension.COUNTRY, as_of=AS_OF)
