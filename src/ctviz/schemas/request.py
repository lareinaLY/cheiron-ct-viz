"""The POST /query request body and its mapping to a QueryPlan.

`query` is read by the LLM planner. Structured fields are optional; any field the
caller sets explicitly overrides what the LLM read from the question. Without an LLM,
the structured fields alone define the query: one cohort plus a dimension or relation.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ctviz.normalize.study import PHASE_LABELS
from ctviz.schemas.plan import (
    MAX_FILTER_TEXT_LENGTH,
    MAX_QUERY_LENGTH,
    MAX_START_YEAR,
    MAX_TOP_N,
    MIN_START_YEAR,
    ChartType,
    Cohort,
    Dimension,
    Filters,
    MatchMode,
    QueryPlan,
    Relation,
)
from ctviz.schemas.trial import OverallStatus, Phase, PrimaryPurpose, StudyType


class QueryRequest(BaseModel):
    """A question plus optional structured fields that pin parts of the plan."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"query": "Compare phases for trials involving pembrolizumab vs nivolumab"},
                {
                    "query": "pembrolizumab trials by phase",
                    "drug": "pembrolizumab",
                    "dimension": "phase",
                },
            ]
        },
    )

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
        description="The user's question, interpreted by the LLM planner when one is "
        "configured. Explicit structured fields below override the LLM's reading.",
    )
    drug: str | None = Field(default=None, max_length=MAX_FILTER_TEXT_LENGTH)
    condition: str | None = Field(default=None, max_length=MAX_FILTER_TEXT_LENGTH)
    sponsor: str | None = Field(default=None, max_length=MAX_FILTER_TEXT_LENGTH)
    country: str | None = Field(default=None, max_length=MAX_FILTER_TEXT_LENGTH)
    phases: list[Phase] = Field(
        default_factory=list,
        max_length=len(Phase),
        description="Any of these phases (OR); repeats are removed.",
    )
    statuses: list[OverallStatus] = Field(
        default_factory=list,
        max_length=len(OverallStatus),
        description="Any of these overall statuses (OR); repeats are removed.",
    )
    start_year_from: int | None = Field(default=None, ge=MIN_START_YEAR, le=MAX_START_YEAR)
    start_year_to: int | None = Field(default=None, ge=MIN_START_YEAR, le=MAX_START_YEAR)
    study_types: list[StudyType] = Field(
        default_factory=list,
        max_length=len(StudyType),
        description="Any of these study types (OR); repeats are removed.",
    )
    exclude_withdrawn: bool = Field(
        default=False, description="Leave out studies withdrawn before enrolling anyone."
    )
    primary_purposes: list[PrimaryPurpose] = Field(
        default_factory=list,
        max_length=len(PrimaryPurpose),
        description="Any of these primary purposes (OR); only interventional studies have "
        "one. Repeats are removed.",
    )
    dimension: Dimension | None = None
    relation: Relation | None = None
    chart_hint: ChartType | None = None
    top_n: int | None = Field(
        default=None, ge=1, le=MAX_TOP_N, description="Keep the N values with the most studies."
    )
    bottom_n: int | None = Field(
        default=None,
        ge=1,
        le=MAX_TOP_N,
        description="Keep the N values with the fewest studies; exclusive with top_n.",
    )
    match_mode: MatchMode = "intervention_name"

    @field_validator("phases", "statuses", "study_types", "primary_purposes")
    @classmethod
    def without_repeats[T](cls, values: list[T]) -> list[T]:
        """An OR list with a repeat means the same; keep the first occurrence."""
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def one_ranking_end(self) -> Self:
        """A ranking keeps one end: top_n or bottom_n, not both."""
        if self.top_n is not None and self.bottom_n is not None:
            raise ValueError("top_n and bottom_n are exclusive; set one of them")
        return self

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, query: str) -> str:
        """A blank question has nothing to interpret; reject it before the LLM is asked."""
        if not query.strip():
            raise ValueError("must contain more than whitespace")
        return query

    def explicit_fields(self) -> set[str]:
        """Names of the structured fields the caller set, defaults excluded."""
        return self.model_fields_set - {"query"}

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
            study_types=self.study_types,
            exclude_withdrawn=self.exclude_withdrawn,
            primary_purposes=self.primary_purposes,
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
            bottom_n=self.bottom_n,
            match_mode=self.match_mode,
        )


STUDY_TYPE_LABELS: dict[StudyType, str] = {
    StudyType.INTERVENTIONAL: "interventional",
    StudyType.OBSERVATIONAL: "observational",
    StudyType.EXPANDED_ACCESS: "expanded access",
}
PRIMARY_PURPOSE_LABELS: dict[PrimaryPurpose, str] = {
    **{p: p.value.replace("_", " ").lower() for p in PrimaryPurpose},
    PrimaryPurpose.ECT: "educational/counseling/training",
}


def cohort_label(filters: Filters, *, with_study_types: bool = True) -> str:
    """A readable, deterministic label, e.g. 'non-small cell lung cancer, recruiting'.

    `with_study_types=False` leaves the study types out, for a title that names them in
    its subject ("Interventional studies by phase for ...").
    """
    parts = [value.strip() for value in (filters.drug, filters.condition) if value]
    if filters.sponsor:
        sponsor = filters.sponsor.strip()
        # query.spons also matches collaborators and any organization name containing the
        # words, so the label says so (a lead-sponsor chart counts far fewer trials).
        parts.append(
            f"sponsored by {sponsor} (lead sponsor or collaborator; name contains '{sponsor}')"
        )
    if filters.country:
        parts.append(f"in {filters.country.strip()}")
    if filters.phases:
        parts.append(" or ".join(PHASE_LABELS[phase] for phase in filters.phases))
    if filters.statuses:
        parts.append(statuses_label(filters))
    types = study_types_label(filters)
    if types and with_study_types:
        parts.append(types)
    if filters.primary_purposes:
        parts.append(primary_purposes_label(filters))
    years = year_range_label(filters.start_year_from, filters.start_year_to)
    if years:
        parts.append(years)
    if filters.exclude_withdrawn:
        parts.append("excluding withdrawn")
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


def count_scope(cohorts: list[Cohort]) -> str:
    """Which studies the counts cover, from the executed filters (meta.count_scope).

    One phrase when every cohort has the same scope, e.g. "interventional studies,
    excluding withdrawn" or "any study type, any status"; otherwise one per cohort,
    "<label>: <scope>", joined with "; ".
    """
    scopes = [cohort_scope(cohort.filters) for cohort in cohorts]
    if len(set(scopes)) == 1:
        return scopes[0]
    return "; ".join(f"{c.label}: {scope}" for c, scope in zip(cohorts, scopes, strict=True))


def cohort_scope(filters: Filters) -> str:
    """Study type, primary purpose and status scope of one cohort's filters."""
    parts = [f"{types} studies" if (types := study_types_label(filters)) else "any study type"]
    if filters.primary_purposes:
        parts.append(primary_purposes_label(filters))
    if filters.statuses:
        parts.append(f"status {statuses_label(filters)}")
    elif filters.exclude_withdrawn:
        parts.append("excluding withdrawn")
    else:
        parts.append("any status")
    return ", ".join(parts)


def study_types_label(filters: Filters) -> str | None:
    """'interventional', 'interventional or observational', or None without a filter."""
    if not filters.study_types:
        return None
    return " or ".join(STUDY_TYPE_LABELS[t] for t in filters.study_types)


def primary_purposes_label(filters: Filters) -> str:
    """'primary purpose treatment or prevention'."""
    return "primary purpose " + " or ".join(
        PRIMARY_PURPOSE_LABELS[p] for p in filters.primary_purposes
    )


def statuses_label(filters: Filters) -> str:
    """'recruiting or not yet recruiting'."""
    return " or ".join(s.value.replace("_", " ").lower() for s in filters.statuses)
