"""Smoke test against the real ClinicalTrials.gov API. Run with: uv run pytest -m live"""

from __future__ import annotations

import pytest

from ctviz import pipeline
from ctviz.config import Settings
from ctviz.retrieval.cache import NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan


@pytest.mark.live
async def test_pembrolizumab_by_phase() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    plan = QueryPlan(
        cohorts=[Cohort(label="pembrolizumab", filters=Filters(drug="pembrolizumab"))],
        dimension=Dimension.PHASE,
    )
    async with CtGovClient(settings, cache=NoCache()) as client:
        response = await pipeline.run(plan, question="pembrolizumab by phase", client=client)

    meta = response.meta
    assert meta.total_matched is not None and meta.total_matched > 0
    assert meta.truncated is False
    assert meta.cohorts[0].reconciled is True
    assert meta.cohorts[0].records_skipped == 0
    assert sum(d.count for d in response.visualization.data) == meta.total_matched
    assert all(d.citations for d in response.visualization.data)
    assert all(d.citation_count_total == d.count for d in response.visualization.data)
