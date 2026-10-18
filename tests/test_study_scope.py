"""Study-scope filters (study type, withdrawn, primary purpose): params, labels, titles,
count scope, validation and planner merging (domain review #3)."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
import respx

from ctviz import pipeline
from ctviz.config import Settings
from ctviz.errors import PlanErrorReason, PlanValidationError
from ctviz.planner.llm import FallbackChain
from ctviz.planner.plan import plan
from ctviz.planner.prompt import build_messages
from ctviz.planner.validation import validate_plan
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.query_builder import (
    build_params,
    search_semantics,
    study_query_params,
    with_bucket_filter,
)
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.request import QueryRequest, cohort_label, count_scope
from ctviz.schemas.trial import OverallStatus, PrimaryPurpose, StudyType
from ctviz.spec.builder import chart_title
from tests.test_llm_fallback import FakeProvider, draft

DRUG_CLAUSE = '(AREA[InterventionName]"pembrolizumab" OR AREA[InterventionMeshTerm]"pembrolizumab")'


def test_primary_purpose_enum_matches_the_api() -> None:
    # GET /studies/enums, type PrimaryPurpose, 2026-10-18.
    assert {p.value for p in PrimaryPurpose} == {
        "TREATMENT",
        "PREVENTION",
        "DIAGNOSTIC",
        "ECT",
        "SUPPORTIVE_CARE",
        "SCREENING",
        "HEALTH_SERVICES_RESEARCH",
        "BASIC_SCIENCE",
        "DEVICE_FEASIBILITY",
        "OTHER",
    }


class TestParams:
    def test_single_study_type(self) -> None:
        params = build_params(Filters(study_types=[StudyType.INTERVENTIONAL]), "intervention_name")
        assert params == {"filter.advanced": "AREA[StudyType]INTERVENTIONAL"}

    def test_several_study_types_are_or_combined(self) -> None:
        filters = Filters(study_types=[StudyType.INTERVENTIONAL, StudyType.OBSERVATIONAL])
        assert build_params(filters, "intervention_name") == {
            "filter.advanced": "AREA[StudyType](INTERVENTIONAL OR OBSERVATIONAL)"
        }

    def test_primary_purposes(self) -> None:
        filters = Filters(primary_purposes=[PrimaryPurpose.TREATMENT, PrimaryPurpose.PREVENTION])
        assert build_params(filters, "intervention_name") == {
            "filter.advanced": "AREA[DesignPrimaryPurpose](TREATMENT OR PREVENTION)"
        }

    def test_exclude_withdrawn_alone(self) -> None:
        # Verified live 2026-10-18: 590,454 + 16,895 withdrawn = 607,349 studies.
        params = build_params(Filters(exclude_withdrawn=True), "intervention_name")
        assert params == {"filter.advanced": "NOT AREA[OverallStatus]WITHDRAWN"}

    def test_all_scope_clauses_are_and_joined_after_the_drug(self) -> None:
        filters = Filters(
            drug="pembrolizumab",
            study_types=[StudyType.INTERVENTIONAL],
            primary_purposes=[PrimaryPurpose.TREATMENT],
            exclude_withdrawn=True,
        )
        assert build_params(filters, "intervention_name")["filter.advanced"] == (
            f"{DRUG_CLAUSE} AND AREA[StudyType]INTERVENTIONAL"
            " AND AREA[DesignPrimaryPurpose]TREATMENT AND NOT AREA[OverallStatus]WITHDRAWN"
        )

    def test_count_route_bucket_keeps_the_scope_clauses(self) -> None:
        filters = Filters(
            drug="pembrolizumab", study_types=[StudyType.INTERVENTIONAL], exclude_withdrawn=True
        )
        params = with_bucket_filter(
            study_query_params(filters, "intervention_name"), "AREA[Phase]PHASE2"
        )
        assert params["filter.advanced"] == (
            f"{DRUG_CLAUSE} AND AREA[StudyType]INTERVENTIONAL"
            " AND NOT AREA[OverallStatus]WITHDRAWN AND (AREA[Phase]PHASE2)"
        )

    def test_defaults_add_nothing(self) -> None:
        assert build_params(Filters(), "intervention_name") == {}

    def test_semantics_describe_each_scope_filter(self) -> None:
        filters = Filters(
            study_types=[StudyType.INTERVENTIONAL],
            primary_purposes=[PrimaryPurpose.TREATMENT],
            exclude_withdrawn=True,
        )
        semantics = search_semantics(filters, "intervention_name")
        assert set(semantics) == {"study_types", "primary_purposes", "exclude_withdrawn"}
        assert "observational" in semantics["primary_purposes"].lower()


class TestBounds:
    def test_repeats_are_removed(self) -> None:
        filters = Filters(study_types=[StudyType.INTERVENTIONAL, StudyType.INTERVENTIONAL])
        assert filters.study_types == [StudyType.INTERVENTIONAL]

    def test_request_fields_reach_the_filters(self) -> None:
        request = QueryRequest(
            query="q",
            drug="pembrolizumab",
            study_types=[StudyType.INTERVENTIONAL],
            exclude_withdrawn=True,
            primary_purposes=[PrimaryPurpose.TREATMENT, PrimaryPurpose.TREATMENT],
            dimension=Dimension.PHASE,
        )
        filters = request.filters()
        assert filters.study_types == [StudyType.INTERVENTIONAL]
        assert filters.exclude_withdrawn is True
        assert filters.primary_purposes == [PrimaryPurpose.TREATMENT]
        assert request.explicit_fields() >= {"study_types", "exclude_withdrawn"}


class TestLabelsAndTitles:
    def test_label_names_every_scope_filter(self) -> None:
        filters = Filters(
            drug="pembrolizumab",
            study_types=[StudyType.INTERVENTIONAL, StudyType.EXPANDED_ACCESS],
            primary_purposes=[PrimaryPurpose.SUPPORTIVE_CARE],
            exclude_withdrawn=True,
        )
        assert cohort_label(filters) == (
            "pembrolizumab, interventional or expanded access, "
            "primary purpose supportive care, excluding withdrawn"
        )

    def test_shared_study_type_becomes_the_title_subject(self) -> None:
        filters = Filters(
            drug="pembrolizumab", study_types=[StudyType.INTERVENTIONAL], exclude_withdrawn=True
        )
        query_plan = QueryPlan(
            cohorts=[Cohort(label=cohort_label(filters), filters=filters)],
            dimension=Dimension.PHASE,
        )
        assert chart_title(query_plan) == (
            "Interventional studies by phase for pembrolizumab, excluding withdrawn"
        )

    def test_differing_study_types_stay_in_the_labels(self) -> None:
        first = Filters(drug="pembrolizumab", study_types=[StudyType.INTERVENTIONAL])
        second = Filters(drug="pembrolizumab", study_types=[StudyType.OBSERVATIONAL])
        query_plan = QueryPlan(
            cohorts=[Cohort(label=cohort_label(f), filters=f) for f in (first, second)],
            dimension=Dimension.PHASE,
        )
        assert chart_title(query_plan) == (
            "Studies by phase: pembrolizumab, interventional vs pembrolizumab, observational"
        )


class TestCountScope:
    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            (Filters(), "any study type, any status"),
            (
                Filters(study_types=[StudyType.INTERVENTIONAL], exclude_withdrawn=True),
                "interventional studies, excluding withdrawn",
            ),
            (
                Filters(statuses=[OverallStatus.RECRUITING]),
                "any study type, status recruiting",
            ),
            (
                Filters(
                    study_types=[StudyType.INTERVENTIONAL],
                    primary_purposes=[PrimaryPurpose.TREATMENT],
                ),
                "interventional studies, primary purpose treatment, any status",
            ),
        ],
    )
    def test_scope_sentence(self, filters: Filters, expected: str) -> None:
        assert count_scope([Cohort(label="c", filters=filters)]) == expected

    def test_differing_cohorts_are_named(self) -> None:
        cohorts = [
            Cohort(label="a", filters=Filters(drug="a", study_types=[StudyType.INTERVENTIONAL])),
            Cohort(label="b", filters=Filters(drug="b")),
        ]
        assert count_scope(cohorts) == (
            "a: interventional studies, any status; b: any study type, any status"
        )


def test_withdrawn_status_with_exclude_withdrawn_is_rejected() -> None:
    filters = Filters(statuses=[OverallStatus.WITHDRAWN], exclude_withdrawn=True)
    query_plan = QueryPlan(cohorts=[Cohort(label="c", filters=filters)], dimension=Dimension.PHASE)
    with pytest.raises(PlanValidationError) as error:
        validate_plan(query_plan)
    assert error.value.reason == PlanErrorReason.CONTRADICTORY_FILTERS


def test_prompt_allows_scope_filters_only_from_explicit_wording() -> None:
    system = build_messages("q", date(2026, 10, 18))[0]["content"]
    assert isinstance(system, str)
    for name in ("study_types", "exclude_withdrawn", "primary_purposes"):
        assert f"`{name}`" in system
    assert "TREATMENT" in system and "EXPANDED_ACCESS" in system


async def test_explicit_scope_fields_override_the_draft() -> None:
    llm_draft = draft(
        cohorts=[Filters(drug="pembrolizumab", study_types=[StudyType.OBSERVATIONAL])]
    )
    chain = FallbackChain([FakeProvider("primary", llm_draft)])
    request: dict[str, Any] = {
        "query": "pembrolizumab by phase",
        "study_types": ["INTERVENTIONAL"],
        "exclude_withdrawn": True,
    }
    interpretation = await plan(QueryRequest(**request), chain, today=date(2026, 10, 18))
    filters = interpretation.plan.cohorts[0].filters
    assert filters.study_types == [StudyType.INTERVENTIONAL]
    assert filters.exclude_withdrawn is True
    assert {o.path for o in interpretation.overrides} == {
        "cohorts[0].filters.study_types",
        "cohorts[0].filters.exclude_withdrawn",
    }


async def test_count_route_requests_carry_the_scope_and_meta_states_it() -> None:
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        advanced = request.url.params.get("filter.advanced", "")
        sent.append(advanced)
        # The cohort matches more than the record cap, so each phase bucket is counted.
        total = 0 if "AREA[Phase]" in advanced else 50
        return httpx.Response(200, json={"studies": [], "totalCount": total})

    filters = Filters(
        drug="pembrolizumab", study_types=[StudyType.INTERVENTIONAL], exclude_withdrawn=True
    )
    query_plan = QueryPlan(
        cohorts=[Cohort(label=cohort_label(filters), filters=filters)], dimension=Dimension.PHASE
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url="https://ctgov.test/api/v2",
        min_request_interval_seconds=0,
        max_records=5,
        cache_enabled=False,
    )
    with respx.mock(base_url="https://ctgov.test/api/v2") as router:
        router.get("/version").mock(return_value=httpx.Response(200, json={}))
        router.get("/studies").mock(side_effect=answer)
        async with CtGovClient(settings, cache=NoCache()) as client:
            response = await pipeline.run(query_plan, question="q", client=client)
    # Every request carries the scope: the cohort's page 1 and each bucket count.
    buckets = [clause for clause in sent if "AREA[Phase]" in clause]
    assert len(buckets) == 10
    assert all(
        "AREA[StudyType]INTERVENTIONAL AND NOT AREA[OverallStatus]WITHDRAWN" in clause
        for clause in sent
    )
    assert response.meta.count_scope == "interventional studies, excluding withdrawn"
    assert response.visualization.title == (
        "Interventional studies by phase for pembrolizumab, excluding withdrawn"
    )
