"""The POST /query request body and its mapping to a QueryPlan.

Until the LLM planner exists, the structured fields alone define the query: they map
to one cohort plus a dimension or relation. `query` is echoed in meta but not read.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ctviz.normalize.study import PHASE_LABELS
from ctviz.schemas.plan import (
    ChartType,
    Cohort,
    Dimension,
    Filters,
    MatchMode,
    QueryPlan,
    Relation,
)
from ctviz.schemas.trial import OverallStatus, Phase


class QueryRequest(BaseModel):
    """A question plus the structured fields that answer it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "pembrolizumab trials by phase",
                    "drug": "pembrolizumab",
                    "dimension": "phase",
                }
            ]
        },
    )

    query: str = Field(
        min_length=1, description="The user's question. Stored in meta; not interpreted yet."
    )
    drug: str | None = None
    condition: str | None = None
    sponsor: str | None = None
    country: str | None = None
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(default_factory=list)
    start_year_from: int | None = None
    start_year_to: int | None = None
    dimension: Dimension | None = None
    relation: Relation | None = None
    chart_hint: ChartType | None = None
    top_n: int | None = None
    match_mode: MatchMode = "intervention_name"

    def filters(self) -> Filters:
        """The request's retrieval filters."""
        return Filters(
            drug=self.drug,
            condition=self.condition,
            sponsor=self.sponsor,
            country=self.country,
            phases=self.phases,
            statuses=self.statuses,
            start_year_from=self.start_year_from,
            start_year_to=self.start_year_to,
        )

    def to_plan(self) -> QueryPlan:
        """One cohort, labelled from its filters, with the requested grouping."""
        filters = self.filters()
        return QueryPlan(
            cohorts=[Cohort(label=cohort_label(filters), filters=filters)],
            dimension=self.dimension,
            relation=self.relation,
            chart_hint=self.chart_hint,
            top_n=self.top_n,
            match_mode=self.match_mode,
        )


def cohort_label(filters: Filters) -> str:
    """A readable, deterministic label, e.g. 'non-small cell lung cancer, recruiting'."""
    parts = [value.strip() for value in (filters.drug, filters.condition) if value]
    if filters.sponsor:
        parts.append(f"sponsored by {filters.sponsor.strip()}")
    if filters.country:
        parts.append(f"in {filters.country.strip()}")
    if filters.phases:
        parts.append(" or ".join(PHASE_LABELS[phase] for phase in filters.phases))
    if filters.statuses:
        parts.append(" or ".join(s.value.replace("_", " ").lower() for s in filters.statuses))
    years = year_range_label(filters.start_year_from, filters.start_year_to)
    if years:
        parts.append(years)
    return ", ".join(parts) or "all trials"


def year_range_label(start: int | None, end: int | None) -> str | None:
    """'started 2015 or later', 'started 2015-2019', 'started in 2020', or None."""
    if start is not None and end is not None:
        return f"started in {start}" if start == end else f"started {start}-{end}"
    if start is not None:
        return f"started {start} or later"
    if end is not None:
        return f"started {end} or earlier"
    return None
