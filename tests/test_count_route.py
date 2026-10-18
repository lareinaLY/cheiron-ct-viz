"""The count-per-bucket route: when a cohort matches more trials than the record cap and
the dimension is API-countable, each bucket is counted exactly with one countTotal request
instead of fetching (and truncating) records."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

import httpx
import pytest
import respx

from ctviz import pipeline
from ctviz.aggregate.registry import DIMENSIONS, UNKNOWN_BUCKET_KEY, bucket_filters
from ctviz.config import CountRoutePolicy, Settings
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.query_builder import intervention_clause
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import (
    DataQuality,
    Interpretation,
    RetrievalRoute,
    VisualizationResponse,
)
from tests.factories import make_raw_study

BASE_URL = "https://ctgov.test/api/v2"
VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-16T09:00:05"}
MAX_RECORDS = 5
SAMPLE_SIZE = 2

PHASE_COUNTS: dict[str, int] = {
    "Early Phase 1": 300,
    "Phase 1": 1_500,
    "Phase 1/Phase 2": 900,
    "Phase 2": 3_000,
    "Phase 2/Phase 3": 150,
    "Phase 3": 1_100,
    "Phase 4": 400,
    "Not Applicable": 6_000,
    UNKNOWN_BUCKET_KEY: 3_563,
}
PHASE_TOTAL = sum(PHASE_COUNTS.values())  # 16,913

PHASES_BY_KEY: dict[str, list[str] | None] = {
    "Early Phase 1": ["EARLY_PHASE1"],
    "Phase 1": ["PHASE1"],
    "Phase 1/Phase 2": ["PHASE1", "PHASE2"],
    "Phase 2": ["PHASE2"],
    "Phase 2/Phase 3": ["PHASE2", "PHASE3"],
    "Phase 3": ["PHASE3"],
    "Phase 4": ["PHASE4"],
    "Not Applicable": ["NA"],
    UNKNOWN_BUCKET_KEY: None,
}

type Sample = Callable[[str, int], dict[str, Any]]


def phase_sample(key: str, index: int) -> dict[str, Any]:
    nct_id = f"NCT{list(PHASES_BY_KEY).index(key):04d}{index:04d}"
    return make_raw_study(nct_id, phases=PHASES_BY_KEY[key])


def year_sample(key: str, index: int) -> dict[str, Any]:
    nct_id = f"NCT{key}{index:04d}"
    if key == UNKNOWN_BUCKET_KEY:
        return make_raw_study(f"NCT0000{index:04d}")
    return make_raw_study(nct_id, start_date={"date": f"{key}-03-01", "type": "ACTUAL"})


class FakeApi:
    """A /studies endpoint that counts per bucket clause and records each request."""

    def __init__(
        self,
        dimension: Dimension,
        counts: dict[str, int],
        *,
        total: int,
        sample: Sample,
        year_range: tuple[int, int] | None = None,
        cohort_clause: str | None = None,
    ) -> None:
        api_count = DIMENSIONS[dimension].api_count
        assert api_count is not None
        self.clauses = dict(
            (clause, key) for key, clause in bucket_filters(api_count, year_range=year_range)
        )
        self.counts = counts
        self.total = total
        self.sample = sample
        self.year_range = year_range
        self.cohort_clause = cohort_clause
        self.requests: list[httpx.Request] = []

    def bucket_key(self, request: httpx.Request) -> str | None:
        advanced = request.url.params.get("filter.advanced")
        if advanced is None:
            return None
        for clause, key in self.clauses.items():
            expected = f"({clause})"
            if self.cohort_clause is not None:
                expected = f"{self.cohort_clause} AND {expected}"
            if advanced == expected:
                return key
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        sort = params.get("sort")
        if sort in ("StartDate:asc", "StartDate:desc"):
            assert self.year_range is not None
            year = self.year_range[0] if sort.endswith("asc") else self.year_range[1]
            study = year_sample(str(year), 0)
            return httpx.Response(200, json={"studies": [study], "totalCount": self.total})
        key = self.bucket_key(request)
        if key is not None:
            count = self.counts.get(key, 0)
            size = min(int(params["pageSize"]), count)
            studies = [self.sample(key, index) for index in range(size)]
            return httpx.Response(200, json={"studies": studies, "totalCount": count})
        page_size = int(params["pageSize"])
        size = min(page_size, self.total)
        studies = [make_raw_study(f"NCT9{index:07d}") for index in range(size)]
        body: dict[str, Any] = {"studies": studies, "totalCount": self.total}
        if page_size < self.total:
            body["nextPageToken"] = "next"
        return httpx.Response(200, json=body)

    def bucket_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if self.bucket_key(r) is not None]

    def fetch_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.params["pageSize"] == str(MAX_RECORDS)]


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/version").mock(return_value=httpx.Response(200, json=VERSION))
        yield router


def install(router: respx.MockRouter, api: FakeApi) -> FakeApi:
    router.get("/studies").mock(side_effect=api)
    return api


def condition_plan(dimension: Dimension = Dimension.PHASE, **fields: Any) -> QueryPlan:
    cohort = Cohort(label="breast cancer", filters=Filters(condition="breast cancer"))
    return QueryPlan(cohorts=[cohort], dimension=dimension, **fields)


async def run(
    query_plan: QueryPlan, policy: CountRoutePolicy | None = None
) -> VisualizationResponse:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ctgov_base_url=BASE_URL,
        max_records=MAX_RECORDS,
        min_request_interval_seconds=0,
        backoff_base_seconds=0,
        max_retries=1,
    )
    async with CtGovClient(settings, cache=NoCache()) as client:
        interpretation = Interpretation(
            question="q", plan=query_plan, planned_by="structured_fields"
        )
        return await pipeline.execute(
            interpretation,
            client=client,
            count_route=policy or CountRoutePolicy(citation_sample_size=SAMPLE_SIZE),
        )


def counts_by_key(response: VisualizationResponse) -> dict[str, int]:
    return {datum.key: datum.count for datum in response.visualization.data}


class TestCountPerBucket:
    async def test_large_countable_cohort_is_counted_exactly(
        self, mock_api: respx.MockRouter
    ) -> None:
        api = install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        response = await run(condition_plan())

        meta = response.meta
        assert counts_by_key(response) == PHASE_COUNTS
        assert meta.retrieval_route == RetrievalRoute.COUNT_PER_BUCKET
        assert meta.data_quality == DataQuality.NORMAL
        assert meta.truncated is False
        assert meta.coverage == 1.0
        assert meta.total_matched == PHASE_TOTAL
        assert response.visualization.title == "Trials by phase for breast cancer"
        [stats] = meta.cohorts
        assert stats.retrieval_route == RetrievalRoute.COUNT_PER_BUCKET
        assert stats.reconciled is True
        assert stats.count_remainder == 0
        assert stats.count_requests == 1 + len(PHASE_COUNTS)
        assert stats.records_retrieved == SAMPLE_SIZE * len(PHASE_COUNTS)
        # Only the fetch's first page, which gave the total; no further page.
        assert len(api.fetch_requests()) == 1
        assert not any("pageToken" in r.url.params for r in api.requests)

    async def test_one_request_per_bucket_with_sample_page_size(
        self, mock_api: respx.MockRouter
    ) -> None:
        api = install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        await run(condition_plan())

        bucket_requests = api.bucket_requests()
        assert sorted(api.bucket_key(r) or "" for r in bucket_requests) == sorted(PHASE_COUNTS)
        for request in bucket_requests:
            params = request.url.params
            assert params["pageSize"] == str(SAMPLE_SIZE)
            assert params["countTotal"] == "true"
            assert params["query.cond"] == "breast cancer"
            assert params["sort"] == "StudyFirstPostDate:desc"
        # The fetch's first page gives the cohort total; it decides the route at no extra cost.
        assert len(api.requests) == 1 + len(PHASE_COUNTS)

    async def test_bucket_clause_is_and_joined_with_cohort_filters(
        self, mock_api: respx.MockRouter
    ) -> None:
        drug_clause = intervention_clause("pembrolizumab")
        api = install(
            mock_api,
            FakeApi(
                Dimension.PHASE,
                PHASE_COUNTS,
                total=PHASE_TOTAL,
                sample=phase_sample,
                cohort_clause=drug_clause,
            ),
        )
        plan = QueryPlan(
            cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
            dimension=Dimension.PHASE,
        )
        response = await run(plan)

        assert len(api.bucket_requests()) == len(PHASE_COUNTS)
        assert counts_by_key(response) == PHASE_COUNTS

    async def test_citations_are_a_sample_of_each_bucket(self, mock_api: respx.MockRouter) -> None:
        install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        response = await run(condition_plan())

        for datum in response.visualization.data:
            assert len(datum.citations) == SAMPLE_SIZE
            assert datum.citation_count_total == datum.count
        phase_2 = next(d for d in response.visualization.data if d.key == "Phase 2")
        assert all(c.value == ["PHASE2"] for c in phase_2.citations)
        unknown = next(d for d in response.visualization.data if d.is_unknown)
        assert all(c.value is None for c in unknown.citations)

    async def test_meta_explains_the_route_and_the_sample(self, mock_api: respx.MockRouter) -> None:
        install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        response = await run(condition_plan())

        [route_note] = [a for a in response.meta.assumptions if "count_per_bucket" in a]
        assert "16,913" in route_note
        assert "record cap (5)" in route_note
        assert "exact" in route_note
        assert f"{SAMPLE_SIZE} most recently registered trials" in route_note
        assert "citation_count_total" in route_note
        assert not any("not a random sample" in a for a in response.meta.assumptions)

    async def test_small_cohort_is_fetched(self, mock_api: respx.MockRouter) -> None:
        api = install(
            mock_api, FakeApi(Dimension.PHASE, {"Phase 1": 3}, total=3, sample=phase_sample)
        )
        response = await run(condition_plan())

        assert response.meta.retrieval_route == RetrievalRoute.FETCH
        assert response.meta.cohorts[0].count_requests is None
        assert api.bucket_requests() == []
        assert len(api.fetch_requests()) == 1

    async def test_forced_route_counts_a_small_cohort(self, mock_api: respx.MockRouter) -> None:
        api = install(
            mock_api,
            FakeApi(Dimension.PHASE, {"Phase 1": 2, "Phase 2": 1}, total=3, sample=phase_sample),
        )
        policy = CountRoutePolicy(citation_sample_size=SAMPLE_SIZE, above_total=0)
        response = await run(condition_plan(), policy)

        assert response.meta.retrieval_route == RetrievalRoute.COUNT_PER_BUCKET
        assert counts_by_key(response) == {"Phase 1": 2, "Phase 2": 1}
        assert len(api.fetch_requests()) == 1

    async def test_top_n_on_a_counted_cohort(self, mock_api: respx.MockRouter) -> None:
        install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        response = await run(condition_plan(top_n=2))

        assert response.meta.top_n is not None
        assert set(counts_by_key(response)) == {"Phase 2", "Not Applicable", UNKNOWN_BUCKET_KEY}
        hidden = sum(
            count
            for key, count in PHASE_COUNTS.items()
            if key not in {"Phase 2", "Not Applicable", UNKNOWN_BUCKET_KEY}
        )
        assert response.meta.top_n.trials_omitted == {"breast cancer": hidden}


class TestRemainder:
    async def test_positive_remainder_falls_back_to_fetch(self, mock_api: respx.MockRouter) -> None:
        api = install(
            mock_api,
            FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL + 50, sample=phase_sample),
        )
        response = await run(condition_plan())

        meta = response.meta
        assert meta.retrieval_route == RetrievalRoute.FETCH
        assert meta.data_quality == DataQuality.PARTIAL
        assert meta.truncated is True
        [stats] = meta.cohorts
        assert stats.count_remainder == 50
        assert stats.count_requests == 1 + len(PHASE_COUNTS)
        [warning] = [w for w in meta.warnings if "count_per_bucket" in w]
        assert "16,913 of 16,963" in warning
        assert "50" in warning
        # The first page that gave the total, then the full fetch from page 1 again.
        assert len(api.fetch_requests()) == 2

    async def test_negative_remainder_falls_back_to_fetch(self, mock_api: respx.MockRouter) -> None:
        install(
            mock_api,
            FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL - 13, sample=phase_sample),
        )
        response = await run(condition_plan())

        assert response.meta.retrieval_route == RetrievalRoute.FETCH
        assert response.meta.cohorts[0].count_remainder == -13
        assert any("count_per_bucket" in w for w in response.meta.warnings)

    async def test_sample_in_the_wrong_bucket_falls_back_to_fetch(
        self, mock_api: respx.MockRouter
    ) -> None:
        def wrong_sample(key: str, index: int) -> dict[str, Any]:
            if key == "Phase 2":
                return make_raw_study(f"NCT7000{index:04d}", phases=["PHASE1", "PHASE2"])
            return phase_sample(key, index)

        api = install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=wrong_sample)
        )
        response = await run(condition_plan())

        assert response.meta.retrieval_route == RetrievalRoute.FETCH
        [warning] = [w for w in response.meta.warnings if "count_per_bucket" in w]
        assert "NCT70000000" in warning
        assert "'Phase 2'" in warning
        assert len(api.fetch_requests()) == 2


class TestNotCounted:
    async def test_non_countable_dimension_is_fetched_and_says_why(
        self, mock_api: respx.MockRouter
    ) -> None:
        api = install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        response = await run(condition_plan(Dimension.COUNTRY))

        assert response.meta.retrieval_route == RetrievalRoute.FETCH
        assert response.meta.truncated is True
        assert api.bucket_requests() == []
        # No count request either: only the paginated fetch.
        assert all(r.url.params["pageSize"] == str(MAX_RECORDS) for r in api.requests)
        assert any(
            "location country cannot be counted per bucket" in a for a in response.meta.assumptions
        )

    async def test_too_many_bucket_requests_falls_back_to_fetch(
        self, mock_api: respx.MockRouter
    ) -> None:
        api = install(
            mock_api, FakeApi(Dimension.PHASE, PHASE_COUNTS, total=PHASE_TOTAL, sample=phase_sample)
        )
        policy = CountRoutePolicy(citation_sample_size=SAMPLE_SIZE, max_bucket_requests=3)
        response = await run(condition_plan(), policy)

        assert response.meta.retrieval_route == RetrievalRoute.FETCH
        assert api.bucket_requests() == []
        [warning] = [w for w in response.meta.warnings if "count_per_bucket" in w]
        assert "9 requests" in warning
        assert "MAX_COUNT_REQUESTS (3)" in warning


class TestStartYear:
    COUNTS: ClassVar[dict[str, int]] = {
        "2024": 4_000,
        "2025": 5_000,
        "2026": 6_000,
        "2027": 900,
        UNKNOWN_BUCKET_KEY: 13,
    }

    def api(self) -> FakeApi:
        return FakeApi(
            Dimension.START_YEAR,
            self.COUNTS,
            total=sum(self.COUNTS.values()),
            sample=year_sample,
            year_range=(2024, 2027),
        )

    async def test_years_between_first_and_last_start_date_are_counted(
        self, mock_api: respx.MockRouter
    ) -> None:
        api = install(mock_api, self.api())
        response = await run(condition_plan(Dimension.START_YEAR))

        assert response.meta.retrieval_route == RetrievalRoute.COUNT_PER_BUCKET
        assert counts_by_key(response) == self.COUNTS
        sorts = [r.url.params.get("sort") for r in api.requests]
        assert "StartDate:asc" in sorts
        assert "StartDate:desc" in sorts
        # total + first + last + one per year + Unknown
        assert response.meta.cohorts[0].count_requests == 1 + 2 + len(self.COUNTS)

    async def test_estimated_counts_are_not_determined(self, mock_api: respx.MockRouter) -> None:
        install(mock_api, self.api())
        response = await run(condition_plan(Dimension.START_YEAR))

        assert response.meta.estimated_count is None
        assert response.meta.unknown_date_type_count is None
        assert all(d.estimated_count is None for d in response.visualization.data)
        assert any("not determined" in a for a in response.meta.assumptions)
