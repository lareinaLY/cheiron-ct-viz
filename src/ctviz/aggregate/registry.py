"""Registry of dimensions and relations.

Each dimension declares how to pull its values from a TrialRecord, where those values
are cited, whether a trial can fall into several buckets, and how buckets are ordered.
`group_by` and `build_network` dispatch through this registry only, so a new query type
is a new entry here, not a new branch elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

from ctviz.schemas.citation import Citation, CitationRole
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import ExclusionReason, NodeType
from ctviz.schemas.trial import UNKNOWN_BUCKET_KEY, EnrollmentType, Phase, TrialRecord

STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"

NON_DRUG_MESH_NOTE = (
    "Intervention MeSH terms are study-level and include non-drug interventions "
    "(e.g. radiation, procedures)."
)
MESH_NOT_LINKED_NOTE = (
    "MeSH terms are not linked to individual interventions; the study's raw intervention "
    "names are cited alongside as supporting citations."
)

NODE_TYPE_NOTE = (
    "Node type 'drug' marks an intervention MeSH term: the API does not link MeSH terms to "
    "intervention types (DRUG, RADIATION, PROCEDURE, ...), so non-drug therapies such as "
    "radiotherapy keep type 'drug' rather than a guessed type."
)


@dataclass(frozen=True)
class ExcludedMesh:
    """A MeSH term that is never drawn as a network node, matched by its stable id."""

    term: str
    reason: ExclusionReason


# Exact MeSH ids observed on live pembrolizumab networks (2026-10-18). Comparators carry no
# active treatment; mis-mappings are derived terms that are not interventions at all
# ("Taxes" from "taxane", "Automobiles" from "CAR"). Diagnostics and procedures are kept:
# a hand-curated list of them would not generalize beyond the queries it was built from.
NETWORK_EXCLUDED_MESH: dict[str, ExcludedMesh] = {
    "D059039": ExcludedMesh("Standard of Care", ExclusionReason.COMPARATOR),
    "D019370": ExcludedMesh("Observation", ExclusionReason.COMPARATOR),
    "D057832": ExcludedMesh("Watchful Waiting", ExclusionReason.COMPARATOR),
    # Placebo arms are mapped to Saline Solution (8 of 18 trials list a "Placebo" intervention).
    "D000077330": ExcludedMesh("Saline Solution", ExclusionReason.COMPARATOR),
    "D013660": ExcludedMesh("Taxes", ExclusionReason.MESH_MISMAPPING),
    "D012847": ExcludedMesh("Single Person", ExclusionReason.MESH_MISMAPPING),
    "D001336": ExcludedMesh("Automobiles", ExclusionReason.MESH_MISMAPPING),
    "D003226": ExcludedMesh("Congresses as Topic", ExclusionReason.MESH_MISMAPPING),
    "D012996": ExcludedMesh("Solutions", ExclusionReason.MESH_MISMAPPING),
}

# Lower bound of each enrollment bin, in order. The last bin is open-ended.
ENROLLMENT_BIN_LOWER_BOUNDS = (0, 1, 50, 100, 500, 1000)

PHASE_RANK = {
    phase: rank
    for rank, phase in enumerate(
        [Phase.EARLY_PHASE1, Phase.PHASE1, Phase.PHASE2, Phase.PHASE3, Phase.PHASE4, Phase.NA]
    )
}


class DimensionKind(StrEnum):
    """What kind of axis a dimension produces; drives chart selection."""

    CATEGORICAL = "categorical"
    TEMPORAL = "temporal"
    BINNED = "binned"


class BucketOrder(StrEnum):
    """How buckets are sorted. The Unknown bucket is always last."""

    BY_COUNT = "by_count"
    BY_KEY = "by_key"
    BY_RANK = "by_rank"


type PathFor = Callable[[int | None], str]


@dataclass(frozen=True)
class DimensionValue:
    """One value a trial has for a dimension, with the citations that prove it."""

    key: str
    label: str
    citations: tuple[Citation, ...]
    estimated: bool = False
    unknown_date_type: bool = False
    is_unknown: bool = False
    rank: tuple[int, ...] = ()


@dataclass(frozen=True)
class DimensionSpec:
    """Declaration of one dimension.

    `label` names the dimension in warnings and assumptions; `title_label` names it in
    chart titles and axis titles, where temporal dimensions state their granularity
    ("start year" rather than "start date"). `description` tells the LLM planner what
    the dimension means and which questions it answers; the planner prompt is rendered
    from it, so prompt and registry cannot drift.
    `source_field` is a key of `TrialRecord.source_paths`; `leaf` is appended after the
    list index (e.g. ".name"). `extract` returns [] when the trial lacks the value,
    which puts it in the Unknown bucket. `time_unit` is the Vega-Lite time unit of a
    temporal dimension's bucket keys.
    """

    label: str
    title_label: str
    kind: DimensionKind
    can_overlap: bool
    source_field: str
    extract: Callable[[TrialRecord, PathFor], list[DimensionValue]]
    order: BucketOrder
    description: str
    leaf: str = ""
    period_key: Callable[[date], str] | None = None
    time_unit: Literal["year", "yearmonth"] | None = None
    assumptions: tuple[str, ...] = field(default_factory=tuple)


def field_path(base: str, leaf: str, index: int | None) -> str:
    """Build a citation path. A "[]" in `base` marks where the list index goes."""
    if index is None:
        return f"{base}{leaf}"
    if "[]" in base:
        return base.replace("[]", f"[{index}]", 1) + leaf
    return f"{base}[{index}]{leaf}"


def path_for(record: TrialRecord, spec: DimensionSpec) -> PathFor:
    """Bind a dimension's citation path to one record."""
    base = record.source_paths[spec.source_field]
    return lambda index: field_path(base, spec.leaf, index)


def cite(
    record: TrialRecord,
    path: str,
    value: str | int | list[str] | None,
    role: CitationRole = CitationRole.PRIMARY,
) -> Citation:
    """A citation of one exact source value."""
    return Citation(
        nct_id=record.nct_id,
        field_path=path,
        value=value,
        url=STUDY_URL.format(nct_id=record.nct_id),
        role=role,
    )


def single(
    record: TrialRecord, key: str, path: str, value: str | int | list[str]
) -> DimensionValue:
    """A one-citation value whose key is also its label."""
    return DimensionValue(key=key, label=key, citations=(cite(record, path, value),))


def extract_phase(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The trial's single phase bucket, citing the raw phases list."""
    if record.phase_bucket == UNKNOWN_BUCKET_KEY:
        return []
    value = DimensionValue(
        key=record.phase_bucket,
        label=record.phase_bucket,
        citations=(cite(record, path(None), [phase.value for phase in record.phases]),),
        rank=tuple(sorted(PHASE_RANK[phase] for phase in record.phases)),
    )
    return [value]


def extract_start_year(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The start year, citing the raw date string; estimated dates are flagged."""
    if record.start_date is None:
        return []
    key = str(record.start_date.year)
    citation = cite(record, path(None), record.start_date_raw)
    return [dated_value(record, key, citation)]


def dated_value(record: TrialRecord, key: str, citation: Citation) -> DimensionValue:
    """A period value flagged with the start date's type (estimated, or no type at all)."""
    return DimensionValue(
        key=key,
        label=key,
        citations=(citation,),
        estimated=record.start_date_type == "estimated",
        unknown_date_type=record.start_date_type == "unknown",
    )


def extract_start_month(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The start month; a year-only date has no month and goes to Unknown."""
    if record.start_date is None:
        return []
    citation = cite(record, path(None), record.start_date_raw)
    if record.start_date_precision == "year":
        # The month is not known; January 1 is only the indexing convention.
        return [
            DimensionValue(
                key=UNKNOWN_BUCKET_KEY,
                label=UNKNOWN_BUCKET_KEY,
                citations=(citation,),
                is_unknown=True,
            )
        ]
    return [dated_value(record, month_key(record.start_date), citation)]


def month_key(day: date) -> str:
    """Bucket key for a month, e.g. '2026-09'."""
    return f"{day.year:04d}-{day.month:02d}"


def year_key(day: date) -> str:
    """Bucket key for a year, e.g. '2026'."""
    return str(day.year)


def extract_country(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """Each distinct country, citing the first location row that lists it."""
    return [
        single(record, location.country, path(location.location_index), location.value_raw)
        for location in record.country_locations
    ]


def extract_sponsor_class(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The lead sponsor's agency class (the API's UNKNOWN is a real value)."""
    if record.lead_sponsor is None or record.lead_sponsor.agency_class is None:
        return []
    value = record.lead_sponsor.agency_class.value
    return [single(record, value, path(None), value)]


def extract_lead_sponsor(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The lead sponsor name."""
    if record.lead_sponsor is None:
        return []
    return [single(record, record.lead_sponsor.name, path(None), record.lead_sponsor.name)]


def extract_intervention_type(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """Each intervention type as listed; types are set per trial, not per drug."""
    return [
        single(record, intervention.type_raw, path(index), intervention.type_raw)
        for index, intervention in enumerate(record.interventions)
        if intervention.type_raw is not None
    ]


def extract_overall_status(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The overall status (the API's UNKNOWN is a real value)."""
    if record.overall_status is None:
        return []
    value = record.overall_status.value
    return [single(record, value, path(None), value)]


def extract_condition(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """Each raw condition string; condition MeSH is not used (spurious terms)."""
    return [
        single(record, condition, path(index), condition)
        for index, condition in enumerate(record.conditions)
    ]


def extract_intervention_mesh(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """Each study-level MeSH term, plus all raw intervention names as context."""
    interventions_path = record.source_paths["interventions"]
    supporting = tuple(
        cite(
            record,
            field_path(interventions_path, ".name", index),
            intervention.name_raw,
            CitationRole.SUPPORTING,
        )
        for index, intervention in enumerate(record.interventions)
    )
    return [
        DimensionValue(
            key=mesh.id or mesh.term,
            label=mesh.term,
            citations=(cite(record, path(index), mesh.term), *supporting),
        )
        for index, mesh in enumerate(record.intervention_mesh_terms)
    ]


def enrollment_bin(count: int) -> tuple[int, str]:
    """(bin index, bin label) for an enrollment count."""
    bounds = ENROLLMENT_BIN_LOWER_BOUNDS
    index = max(i for i, lower in enumerate(bounds) if count >= lower)
    lower = bounds[index]
    if index == len(bounds) - 1:
        return index, f"{lower}+"
    upper = bounds[index + 1] - 1
    return index, str(lower) if lower == upper else f"{lower}-{upper}"


def extract_enrollment_bucket(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The enrollment bin, citing the raw count; estimated counts are flagged."""
    if record.enrollment is None:
        return []
    index, key = enrollment_bin(record.enrollment)
    value = DimensionValue(
        key=key,
        label=key,
        citations=(cite(record, path(None), record.enrollment),),
        estimated=record.enrollment_type == EnrollmentType.ESTIMATED,
        rank=(index,),
    )
    return [value]


DIMENSIONS: dict[Dimension, DimensionSpec] = {
    Dimension.PHASE: DimensionSpec(
        label="phase",
        title_label="phase",
        description=(
            "Trial phase (Early Phase 1, Phase 1 to 4, Not Applicable); multi-phase "
            "trials such as 'Phase 1/Phase 2' form their own bucket. For 'by phase' or "
            "'phase breakdown'."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=False,
        source_field="phases",
        extract=extract_phase,
        order=BucketOrder.BY_RANK,
        assumptions=(
            "Multi-phase trials form their own bucket (e.g. 'Phase 1/Phase 2'); "
            "phase buckets are mutually exclusive.",
        ),
    ),
    Dimension.START_YEAR: DimensionSpec(
        label="start date",
        title_label="start year",
        description=(
            "Year the trial started. For trends over time: 'per year', 'each year', 'over"
            " time', 'since 2015'."
        ),
        kind=DimensionKind.TEMPORAL,
        can_overlap=False,
        source_field="start_date",
        extract=extract_start_year,
        order=BucketOrder.BY_KEY,
        period_key=year_key,
        time_unit="year",
    ),
    Dimension.START_MONTH: DimensionSpec(
        label="start month",
        title_label="start month",
        description=(
            "Month the trial started. Only when the user asks for monthly granularity "
            "('per month', 'each month')."
        ),
        kind=DimensionKind.TEMPORAL,
        can_overlap=False,
        source_field="start_date",
        extract=extract_start_month,
        order=BucketOrder.BY_KEY,
        period_key=month_key,
        time_unit="yearmonth",
    ),
    Dimension.COUNTRY: DimensionSpec(
        label="location country",
        title_label="location country",
        description=(
            "Countries with at least one trial site; a trial with sites in several "
            "countries counts in each. For 'which countries', 'where', geographic "
            "distribution."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=True,
        source_field="countries",
        extract=extract_country,
        order=BucketOrder.BY_COUNT,
        assumptions=(
            "Countries are reported as the API gives them; territories are not rolled up.",
        ),
    ),
    Dimension.SPONSOR_CLASS: DimensionSpec(
        label="lead sponsor class",
        title_label="lead sponsor class",
        description=(
            "Type of lead sponsor: industry, NIH, other government, network, individual, "
            "other (e.g. academic). For 'industry vs academic' or 'who funds'."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=False,
        source_field="lead_sponsor",
        leaf=".class",
        extract=extract_sponsor_class,
        order=BucketOrder.BY_COUNT,
    ),
    Dimension.LEAD_SPONSOR: DimensionSpec(
        label="lead sponsor",
        title_label="lead sponsor",
        description=(
            "Lead sponsor organization (a company, university or agency). For 'which "
            "sponsors' or 'top companies'."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=False,
        source_field="lead_sponsor",
        leaf=".name",
        extract=extract_lead_sponsor,
        order=BucketOrder.BY_COUNT,
    ),
    Dimension.INTERVENTION_TYPE: DimensionSpec(
        label="intervention type",
        title_label="intervention type",
        description=(
            "Intervention type as listed (drug, biological, device, procedure, radiation,"
            " behavioral, ...); a trial with several types counts in each."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=True,
        source_field="interventions",
        leaf=".type",
        extract=extract_intervention_type,
        order=BucketOrder.BY_COUNT,
    ),
    Dimension.OVERALL_STATUS: DimensionSpec(
        label="overall status",
        title_label="overall status",
        description=(
            "Overall recruitment status (recruiting, completed, terminated, withdrawn, "
            "...). For 'by status'."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=False,
        source_field="overall_status",
        extract=extract_overall_status,
        order=BucketOrder.BY_COUNT,
    ),
    Dimension.CONDITION: DimensionSpec(
        label="condition",
        title_label="condition",
        description=(
            "Conditions studied, as entered by the sponsor (not normalized). For 'which "
            "diseases' or 'which conditions'."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=True,
        source_field="conditions",
        extract=extract_condition,
        order=BucketOrder.BY_COUNT,
        assumptions=("Conditions are the raw sponsor-entered strings, not MeSH-normalized.",),
    ),
    Dimension.INTERVENTION_MESH: DimensionSpec(
        label="intervention MeSH term",
        title_label="intervention MeSH term",
        description=(
            "Interventions (drugs and other treatments) grouped by MeSH term; a trial "
            "counts once per term. For 'which drugs' or 'most common treatments'."
        ),
        kind=DimensionKind.CATEGORICAL,
        can_overlap=True,
        source_field="intervention_mesh_terms",
        leaf=".term",
        extract=extract_intervention_mesh,
        order=BucketOrder.BY_COUNT,
        assumptions=(NON_DRUG_MESH_NOTE, MESH_NOT_LINKED_NOTE),
    ),
    Dimension.ENROLLMENT_BUCKET: DimensionSpec(
        label="enrollment count",
        title_label="enrollment count",
        description=(
            "Enrollment size (actual or planned participants) in bins 0, 1-49, 50-99, "
            "100-499, 500-999, 1000+. For 'trial sizes' or 'how many participants'."
        ),
        kind=DimensionKind.BINNED,
        can_overlap=False,
        source_field="enrollment",
        extract=extract_enrollment_bucket,
        order=BucketOrder.BY_RANK,
        assumptions=("Enrollment bins: 0, 1-49, 50-99, 100-499, 500-999, 1000+.",),
    ),
}


@dataclass(frozen=True)
class RelationSpec:
    """Declaration of one relation: two dimensions whose values become linked nodes.

    `label` names the relation in chart titles; `description` is rendered into the
    planner prompt.

    When both sides are the same dimension, edges are the unordered pairs of distinct
    values within a trial; otherwise every left value links to every right value.
    `excluded` maps a node type to the value keys that never become nodes of that type.
    """

    label: str
    left: Dimension
    left_type: NodeType
    right: Dimension
    right_type: NodeType
    description: str
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    excluded: dict[NodeType, dict[str, ExcludedMesh]] = field(default_factory=dict)

    @property
    def same_dimension(self) -> bool:
        """True when edges link values of one dimension to each other (co-occurrence)."""
        return self.left == self.right


RELATIONS: dict[Relation, RelationSpec] = {
    Relation.SPONSOR_DRUG: RelationSpec(
        label="lead sponsor-drug",
        description=(
            "Network linking lead sponsors to the drugs (intervention MeSH terms) in "
            "their trials. For 'which sponsors work on which drugs'."
        ),
        left=Dimension.LEAD_SPONSOR,
        left_type=NodeType.SPONSOR,
        right=Dimension.INTERVENTION_MESH,
        right_type=NodeType.DRUG,
        assumptions=(
            "Sponsors are lead sponsors only; collaborators are not included.",
            NON_DRUG_MESH_NOTE,
            NODE_TYPE_NOTE,
        ),
        excluded={NodeType.DRUG: NETWORK_EXCLUDED_MESH},
    ),
    Relation.DRUG_DRUG_COOCCURRENCE: RelationSpec(
        label="drug co-occurrence",
        description=(
            "Network of drugs tested together in the same trial. For 'what is X combined "
            "with' or 'drug combinations'."
        ),
        left=Dimension.INTERVENTION_MESH,
        left_type=NodeType.DRUG,
        right=Dimension.INTERVENTION_MESH,
        right_type=NodeType.DRUG,
        assumptions=(NON_DRUG_MESH_NOTE, NODE_TYPE_NOTE),
        excluded={NodeType.DRUG: NETWORK_EXCLUDED_MESH},
    ),
    Relation.CONDITION_DRUG: RelationSpec(
        label="condition-drug",
        description=(
            "Network linking conditions to the drugs studied for them. For 'which drugs "
            "are tested for which conditions'."
        ),
        left=Dimension.CONDITION,
        left_type=NodeType.CONDITION,
        right=Dimension.INTERVENTION_MESH,
        right_type=NodeType.DRUG,
        assumptions=(
            "Conditions are the raw sponsor-entered strings, not MeSH-normalized.",
            NON_DRUG_MESH_NOTE,
            NODE_TYPE_NOTE,
        ),
        excluded={NodeType.DRUG: NETWORK_EXCLUDED_MESH},
    ),
}


def distinct_values(record: TrialRecord, spec: DimensionSpec) -> list[DimensionValue]:
    """A trial's values for a dimension, keeping the first occurrence of each key."""
    seen: dict[str, DimensionValue] = {}
    for value in spec.extract(record, path_for(record, spec)):
        seen.setdefault(value.key, value)
    return list(seen.values())
