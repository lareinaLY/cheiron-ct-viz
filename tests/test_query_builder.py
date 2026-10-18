"""Tests for retrieval.query_builder (QueryPlan filters -> API params)."""

from __future__ import annotations

import pytest

from ctviz.errors import InvalidFilterError
from ctviz.retrieval.query_builder import build_params, search_semantics, study_query_params
from ctviz.schemas.plan import Filters
from ctviz.schemas.trial import OverallStatus, Phase


class TestDrug:
    def test_intervention_name_mode_uses_quoted_area_filter(self) -> None:
        params = build_params(Filters(drug="pembrolizumab"), "intervention_name")
        assert params == {"filter.advanced": 'AREA[InterventionName]"pembrolizumab"'}

    def test_broad_mode_uses_query_intr(self) -> None:
        params = build_params(Filters(drug="pembrolizumab"), "broad")
        assert params == {"query.intr": "pembrolizumab"}

    def test_name_with_spaces_and_parentheses_is_quoted_as_one_phrase(self) -> None:
        # Unquoted, the API parses "(KEYTRUDA®)" as grouping: 559 hits instead of 43.
        params = build_params(Filters(drug="pembrolizumab (KEYTRUDA®)"), "intervention_name")
        assert params == {"filter.advanced": 'AREA[InterventionName]"pembrolizumab (KEYTRUDA®)"'}

    def test_double_quote_in_intervention_name_rejected(self) -> None:
        with pytest.raises(InvalidFilterError, match="drug"):
            build_params(Filters(drug='foo "bar"'), "intervention_name")

    @pytest.mark.parametrize(
        "value", ["pembrolizumab) OR (nivolumab", "pembrolizumab AND nivolumab", "NOT placebo"]
    )
    def test_operator_in_intervention_name_rejected(self, value: str) -> None:
        with pytest.raises(InvalidFilterError, match="operator"):
            build_params(Filters(drug=value), "intervention_name")

    def test_lowercase_operator_word_in_intervention_name_allowed(self) -> None:
        params = build_params(Filters(drug="salt and pepper"), "intervention_name")
        assert params == {"filter.advanced": 'AREA[InterventionName]"salt and pepper"'}

    def test_value_is_trimmed(self) -> None:
        params = build_params(Filters(drug="  pembrolizumab "), "intervention_name")
        assert params == {"filter.advanced": 'AREA[InterventionName]"pembrolizumab"'}

    def test_blank_value_rejected(self) -> None:
        with pytest.raises(InvalidFilterError, match="drug"):
            build_params(Filters(drug="   "), "intervention_name")


class TestFreeTextFilters:
    @pytest.mark.parametrize(
        ("filters", "param", "value"),
        [
            (
                Filters(condition="non-small cell lung cancer"),
                "query.cond",
                "non-small cell lung cancer",
            ),
            (Filters(sponsor="Merck Sharp & Dohme LLC"), "query.spons", "Merck Sharp & Dohme LLC"),
            (Filters(country="United States"), "query.locn", "United States"),
        ],
        ids=["condition", "sponsor", "country"],
    )
    def test_maps_to_raw_query_param(self, filters: Filters, param: str, value: str) -> None:
        assert build_params(filters, "intervention_name") == {param: value}

    @pytest.mark.parametrize(
        "value",
        [
            '"lung cancer"',
            "lung (cancer)",
            "AREA[Phase]PHASE3",
            "lung cancer OR melanoma",
            "lung AND cancer",
            "cancer NOT melanoma",
        ],
        ids=["quotes", "parentheses", "brackets", "or", "and", "not"],
    )
    def test_essie_syntax_rejected(self, value: str) -> None:
        with pytest.raises(InvalidFilterError, match="condition"):
            build_params(Filters(condition=value), "intervention_name")

    @pytest.mark.parametrize("value", ["Oregon Health and Science University", "Notre Dame"])
    def test_lowercase_or_embedded_operator_words_allowed(self, value: str) -> None:
        assert build_params(Filters(sponsor=value), "intervention_name") == {"query.spons": value}

    def test_broad_drug_is_checked_for_essie_syntax(self) -> None:
        with pytest.raises(InvalidFilterError, match="drug"):
            build_params(Filters(drug="pembrolizumab (KEYTRUDA®)"), "broad")


class TestEnumFilters:
    def test_single_phase(self) -> None:
        params = build_params(Filters(phases=[Phase.PHASE2]), "intervention_name")
        assert params == {"filter.advanced": "AREA[Phase]PHASE2"}

    def test_several_phases_are_or_combined(self) -> None:
        params = build_params(Filters(phases=[Phase.PHASE2, Phase.PHASE3]), "intervention_name")
        assert params == {"filter.advanced": "AREA[Phase](PHASE2 OR PHASE3)"}

    def test_statuses_use_pipe_separated_filter(self) -> None:
        filters = Filters(statuses=[OverallStatus.RECRUITING, OverallStatus.NOT_YET_RECRUITING])
        params = build_params(filters, "intervention_name")
        assert params == {"filter.overallStatus": "RECRUITING|NOT_YET_RECRUITING"}


class TestStartYears:
    def test_from_only(self) -> None:
        params = build_params(Filters(start_year_from=2015), "intervention_name")
        assert params == {"filter.advanced": "AREA[StartDate]RANGE[2015-01-01,MAX]"}

    def test_to_only(self) -> None:
        params = build_params(Filters(start_year_to=2019), "intervention_name")
        assert params == {"filter.advanced": "AREA[StartDate]RANGE[MIN,2019-12-31]"}

    def test_both_bounds_inclusive(self) -> None:
        params = build_params(
            Filters(start_year_from=2015, start_year_to=2019), "intervention_name"
        )
        assert params == {"filter.advanced": "AREA[StartDate]RANGE[2015-01-01,2019-12-31]"}


class TestCombination:
    def test_all_filters_combined_with_and(self) -> None:
        filters = Filters(
            drug="pembrolizumab",
            condition="melanoma",
            sponsor="Merck Sharp & Dohme LLC",
            country="Germany",
            phases=[Phase.PHASE2, Phase.PHASE3],
            statuses=[OverallStatus.RECRUITING],
            start_year_from=2015,
        )
        assert build_params(filters, "intervention_name") == {
            "query.cond": "melanoma",
            "query.spons": "Merck Sharp & Dohme LLC",
            "query.locn": "Germany",
            "filter.overallStatus": "RECRUITING",
            "filter.advanced": (
                'AREA[InterventionName]"pembrolizumab" AND AREA[Phase](PHASE2 OR PHASE3) '
                "AND AREA[StartDate]RANGE[2015-01-01,MAX]"
            ),
        }

    def test_no_filters_gives_no_params(self) -> None:
        assert build_params(Filters(), "intervention_name") == {}


class TestSearchSemantics:
    def test_describes_only_filters_in_use(self) -> None:
        semantics = search_semantics(Filters(drug="x", phases=[Phase.PHASE2]), "intervention_name")
        assert set(semantics) == {"drug", "phases"}
        assert "InterventionName" in semantics["drug"]
        assert "any" in semantics["phases"]

    def test_broad_drug_semantics_mention_free_text(self) -> None:
        semantics = search_semantics(Filters(drug="x"), "broad")
        assert "query.intr" in semantics["drug"]

    @pytest.mark.parametrize("field", ["condition", "sponsor", "country", "statuses"])
    def test_each_filter_described(self, field: str) -> None:
        values = {"statuses": [OverallStatus.RECRUITING]}
        filters = Filters(**{field: values.get(field, "x")})
        assert set(search_semantics(filters, "intervention_name")) == {field}

    def test_start_year_semantics_mention_missing_dates_excluded(self) -> None:
        semantics = search_semantics(Filters(start_year_from=2015), "intervention_name")
        assert "no start date" in semantics["start_year"]


class TestStudyQueryParams:
    """Every /studies query pins its order, so a capped fetch is a describable subset."""

    @pytest.mark.parametrize(
        ("filters", "match_mode"),
        [
            (Filters(), "intervention_name"),
            (Filters(drug="pembrolizumab"), "intervention_name"),
            (Filters(drug="pembrolizumab"), "broad"),
            (Filters(condition="breast cancer", phases=[Phase.PHASE3]), "intervention_name"),
        ],
        ids=["no-filters", "drug", "broad-drug", "condition-and-phase"],
    )
    def test_always_sorts_by_first_post_date_descending(
        self, filters: Filters, match_mode: str
    ) -> None:
        params = study_query_params(filters, match_mode)  # type: ignore[arg-type]
        assert params["sort"] == "StudyFirstPostDate:desc"
        assert {k: v for k, v in params.items() if k != "sort"} == build_params(
            filters,
            match_mode,  # type: ignore[arg-type]
        )

    def test_filter_errors_still_raise(self) -> None:
        with pytest.raises(InvalidFilterError, match="condition"):
            study_query_params(Filters(condition="cancer OR diabetes"), "intervention_name")
