"""Reconciliation of the two retrieval routes against the real API.

Pembrolizumab fits under the record cap, so the fetch route counts every trial exactly.
Forcing the count_per_bucket route on the same cohort must give the same number in every
bucket. A disagreement means our reading of the API's filter semantics (e.g. the exact
phase-combination clauses) is wrong: it is a finding to report with the bucket and both
numbers, never something to tune away. Run with: uv run pytest -m live
"""

from __future__ import annotations

import pytest

from ctviz import pipeline
from ctviz.config import CountRoutePolicy, Settings
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan
from ctviz.schemas.response import RetrievalRoute, VisualizationResponse

FORCE_COUNT_ROUTE = CountRoutePolicy(above_total=0)


def pembrolizumab_plan(dimension: Dimension) -> QueryPlan:
    return QueryPlan(
        cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
        dimension=dimension,
    )


def counts(response: VisualizationResponse) -> dict[str, int]:
    return {datum.key: datum.count for datum in response.visualization.data}


def disagreements(fetched: dict[str, int], counted: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Bucket -> (fetch route count, count route count) wherever the two differ."""
    return {
        key: (fetched.get(key, 0), counted.get(key, 0))
        for key in sorted(set(fetched) | set(counted))
        if fetched.get(key, 0) != counted.get(key, 0)
    }


@pytest.mark.live
@pytest.mark.parametrize(
    "dimension",
    [Dimension.PHASE, Dimension.START_YEAR, Dimension.OVERALL_STATUS, Dimension.SPONSOR_CLASS],
)
async def test_count_route_matches_fetch_route(dimension: Dimension) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    plan = pembrolizumab_plan(dimension)
    async with CtGovClient(settings, cache=NoCache()) as client:
        fetched = await pipeline.run(plan, question="reconcile (fetch)", client=client)
        counted = await pipeline.run(
            plan, question="reconcile (count)", client=client, count_route=FORCE_COUNT_ROUTE
        )

    assert fetched.meta.retrieval_route == RetrievalRoute.FETCH
    assert fetched.meta.truncated is False
    assert counted.meta.retrieval_route == RetrievalRoute.COUNT_PER_BUCKET, counted.meta.warnings
    assert fetched.meta.data_timestamp == counted.meta.data_timestamp
    assert fetched.meta.total_matched == counted.meta.total_matched
    assert counted.meta.cohorts[0].count_remainder == 0
    # fetch count, count-route count
    assert disagreements(counts(fetched), counts(counted)) == {}
    assert all(datum.citations for datum in counted.visualization.data)
