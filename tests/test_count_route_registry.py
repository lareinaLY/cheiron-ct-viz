"""Registry declarations for the count-per-bucket route: which dimensions the API can count
exactly, and the filter clause that selects exactly one bucket."""

from __future__ import annotations

import pytest

from ctviz.aggregate.registry import DIMENSIONS, UNKNOWN_BUCKET_KEY, bucket_filters
from ctviz.normalize.study import phase_bucket
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import AgencyClass, OverallStatus, Phase

COUNTABLE = {
    Dimension.PHASE,
    Dimension.OVERALL_STATUS,
    Dimension.START_YEAR,
    Dimension.SPONSOR_CLASS,
}

PHASE_KEYS = [
    "Early Phase 1",
    "Phase 1",
    "Phase 1/Phase 2",
    "Phase 2",
    "Phase 2/Phase 3",
    "Phase 3",
    "Phase 4",
    "Not Applicable",
]


def api_count(dimension: Dimension):  # type: ignore[no-untyped-def]
    spec = DIMENSIONS[dimension].api_count
    assert spec is not None, f"{dimension} should be API-countable"
    return spec


class TestWhichDimensionsAreCountable:
    def test_countable_dimensions_declare_an_api_count(self) -> None:
        declared = {dim for dim, spec in DIMENSIONS.items() if spec.api_count is not None}
        assert declared == COUNTABLE

    @pytest.mark.parametrize(
        "dimension",
        [
            Dimension.COUNTRY,
            Dimension.CONDITION,
            Dimension.INTERVENTION_MESH,
            Dimension.INTERVENTION_TYPE,
            Dimension.LEAD_SPONSOR,
            Dimension.ENROLLMENT_BUCKET,
            Dimension.START_MONTH,
        ],
    )
    def test_other_dimensions_stay_fetch_only(self, dimension: Dimension) -> None:
        assert DIMENSIONS[dimension].api_count is None

    @pytest.mark.parametrize("dimension", sorted(COUNTABLE))
    def test_countable_dimensions_are_single_valued(self, dimension: Dimension) -> None:
        # Per-bucket counts only add up to the cohort total when buckets are exclusive.
        assert DIMENSIONS[dimension].can_overlap is False


class TestPhase:
    def test_keys_are_every_single_phase_and_the_observed_combinations(self) -> None:
        assert list(api_count(Dimension.PHASE).keys) == PHASE_KEYS

    def test_keys_match_the_normalizer_labels(self) -> None:
        assert phase_bucket([Phase.PHASE1, Phase.PHASE2]) in PHASE_KEYS
        assert phase_bucket([Phase.PHASE2, Phase.PHASE3]) in PHASE_KEYS
        for phase in Phase:
            assert phase_bucket([phase]) in PHASE_KEYS

    def test_single_phase_excludes_every_other_phase(self) -> None:
        clause = api_count(Dimension.PHASE).api_filter("Phase 2")
        assert clause == (
            "AREA[Phase]PHASE2 "
            "AND NOT AREA[Phase](NA OR EARLY_PHASE1 OR PHASE1 OR PHASE3 OR PHASE4)"
        )

    def test_combined_bucket_requires_both_phases_and_excludes_the_rest(self) -> None:
        clause = api_count(Dimension.PHASE).api_filter("Phase 1/Phase 2")
        assert clause == (
            "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2 "
            "AND NOT AREA[Phase](NA OR EARLY_PHASE1 OR PHASE3 OR PHASE4)"
        )

    def test_not_applicable_is_the_na_value(self) -> None:
        clause = api_count(Dimension.PHASE).api_filter("Not Applicable")
        assert clause.startswith("AREA[Phase]NA AND NOT AREA[Phase](EARLY_PHASE1 OR ")

    def test_missing_phase_is_counted_with_missing(self) -> None:
        assert api_count(Dimension.PHASE).missing_filter == "AREA[Phase]MISSING"

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(KeyError):
            api_count(Dimension.PHASE).api_filter("Phase 1/Phase 3")


class TestOtherDimensions:
    def test_overall_status(self) -> None:
        spec = api_count(Dimension.OVERALL_STATUS)
        assert list(spec.keys) == [status.value for status in OverallStatus]
        assert spec.api_filter("RECRUITING") == "AREA[OverallStatus]RECRUITING"
        assert spec.missing_filter == "AREA[OverallStatus]MISSING"

    def test_sponsor_class(self) -> None:
        spec = api_count(Dimension.SPONSOR_CLASS)
        assert list(spec.keys) == [agency_class.value for agency_class in AgencyClass]
        assert spec.api_filter("INDUSTRY") == "AREA[LeadSponsorClass]INDUSTRY"
        # The API's own UNKNOWN class is a value, not the missing bucket.
        assert spec.api_filter("UNKNOWN") == "AREA[LeadSponsorClass]UNKNOWN"
        assert spec.missing_filter == "AREA[LeadSponsorClass]MISSING"

    def test_enum_keys_reject_values_outside_the_enum(self) -> None:
        with pytest.raises(KeyError):
            api_count(Dimension.OVERALL_STATUS).api_filter("PAUSED")

    def test_start_year_is_an_inclusive_whole_year_range(self) -> None:
        spec = api_count(Dimension.START_YEAR)
        assert spec.keys == ()
        assert spec.year_range_field == "StartDate"
        assert spec.api_filter("2020") == "AREA[StartDate]RANGE[2020-01-01,2020-12-31]"
        assert spec.missing_filter == "AREA[StartDate]MISSING"

    def test_start_year_rejects_a_key_that_is_not_a_year(self) -> None:
        with pytest.raises(KeyError):
            api_count(Dimension.START_YEAR).api_filter("2020-01")


class TestBucketFilters:
    def test_enum_dimension_lists_every_key_then_unknown(self) -> None:
        filters = bucket_filters(api_count(Dimension.PHASE), year_range=None)
        assert [key for key, _ in filters] == [*PHASE_KEYS, UNKNOWN_BUCKET_KEY]
        assert filters[-1] == (UNKNOWN_BUCKET_KEY, "AREA[Phase]MISSING")

    def test_year_dimension_lists_every_year_in_range_then_unknown(self) -> None:
        filters = bucket_filters(api_count(Dimension.START_YEAR), year_range=(2019, 2021))
        assert filters == [
            ("2019", "AREA[StartDate]RANGE[2019-01-01,2019-12-31]"),
            ("2020", "AREA[StartDate]RANGE[2020-01-01,2020-12-31]"),
            ("2021", "AREA[StartDate]RANGE[2021-01-01,2021-12-31]"),
            (UNKNOWN_BUCKET_KEY, "AREA[StartDate]MISSING"),
        ]

    def test_year_dimension_without_any_dated_trial_counts_only_unknown(self) -> None:
        filters = bucket_filters(api_count(Dimension.START_YEAR), year_range=None)
        assert filters == [(UNKNOWN_BUCKET_KEY, "AREA[StartDate]MISSING")]
