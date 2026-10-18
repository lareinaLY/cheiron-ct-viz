"""Registry of dimensions and relations.

Each dimension declares how to pull its values from a TrialRecord, where those values
are cited, whether a trial can fall into several buckets, and how buckets are ordered.
`group_by` and `build_network` dispatch through this registry only, so a new query type
is a new entry here, not a new branch elsewhere.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

from ctviz.aggregate.mesh_filter import MeshNodeFilter
from ctviz.data import MESH_TREES_RELEASE, load_mesh_trees
from ctviz.normalize.study import PHASE_LABELS, phase_bucket
from ctviz.schemas.citation import Citation, CitationRole
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import NodeType
from ctviz.schemas.trial import (
    PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY,
    UNKNOWN_BUCKET_KEY,
    UNRECOGNIZED_BUCKET_KEY,
    AgencyClass,
    EnrollmentType,
    InterventionType,
    OverallStatus,
    Phase,
    TrialRecord,
    UnrecognizedValue,
)

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
    "intervention types (DRUG, RADIATION, PROCEDURE, ...), so drug nodes are chosen by MeSH "
    "tree branch instead of by intervention type."
)
DRUG_NODE_NOTE = (
    "Drug nodes are intervention MeSH terms filed under Chemicals and Drugs: supplementary "
    "concepts (ids starting with C) and descriptors with a MeSH tree number in branch D "
    f"({MESH_TREES_RELEASE} trees); non-drug interventions such as radiotherapy (E02), "
    "procedures and diagnostics are not drug nodes."
)


# Branch-D descriptors that carry no active treatment. Everything else outside branch D is
# dropped by the tree rule, so this list only needs comparators the rule would keep: placebo
# arms are mapped to Saline Solution (D26.776; 8 of 18 trials in the pembrolizumab network
# list a "Placebo" intervention), and Placebos itself is D26.660.
NETWORK_COMPARATOR_MESH: dict[str, str] = {
    "D000077330": "Saline Solution",
    "D010919": "Placebos",
}

DRUG_NODE_FILTER = MeshNodeFilter(trees=load_mesh_trees(), comparators=NETWORK_COMPARATOR_MESH)

# Lower bound of each enrollment bin, in order. The last bin is open-ended.
ENROLLMENT_BIN_LOWER_BOUNDS = (0, 1, 50, 100, 500, 1000)

# Display labels for enum buckets; the bucket key and the citation keep the registry code.
# Wording follows the ClinicalTrials.gov website. The registry's UNKNOWN status ("status
# not verified for over two years") is a real value, so its label says so to keep it apart
# from the Unknown (missing) bucket.
OVERALL_STATUS_LABELS: dict[OverallStatus, str] = {
    OverallStatus.ACTIVE_NOT_RECRUITING: "Active, not recruiting",
    OverallStatus.COMPLETED: "Completed",
    OverallStatus.ENROLLING_BY_INVITATION: "Enrolling by invitation",
    OverallStatus.NOT_YET_RECRUITING: "Not yet recruiting",
    OverallStatus.RECRUITING: "Recruiting",
    OverallStatus.SUSPENDED: "Suspended",
    OverallStatus.TERMINATED: "Terminated",
    OverallStatus.WITHDRAWN: "Withdrawn (never enrolled)",
    OverallStatus.AVAILABLE: "Available (expanded access)",
    OverallStatus.NO_LONGER_AVAILABLE: "No longer available (expanded access)",
    OverallStatus.TEMPORARILY_NOT_AVAILABLE: "Temporarily not available (expanded access)",
    OverallStatus.APPROVED_FOR_MARKETING: "Approved for marketing (expanded access)",
    OverallStatus.WITHHELD: "Withheld",
    OverallStatus.UNKNOWN: "Unknown status (registry value)",
}
AGENCY_CLASS_LABELS: dict[AgencyClass, str] = {
    AgencyClass.NIH: "NIH",
    AgencyClass.FED: "U.S. federal (non-NIH)",
    AgencyClass.OTHER_GOV: "Other government",
    AgencyClass.INDIV: "Individual",
    AgencyClass.INDUSTRY: "Industry",
    AgencyClass.NETWORK: "Network (cooperative group)",
    AgencyClass.AMBIG: "Ambiguous (registry value)",
    AgencyClass.OTHER: "Other (academic, hospital, foundation)",
    AgencyClass.UNKNOWN: "Unknown class (registry value)",
}

INTERVENTION_TYPE_LABELS: dict[InterventionType, str] = {
    InterventionType.BEHAVIORAL: "Behavioral",
    InterventionType.BIOLOGICAL: "Biological",
    InterventionType.COMBINATION_PRODUCT: "Combination product",
    InterventionType.DEVICE: "Device",
    InterventionType.DIAGNOSTIC_TEST: "Diagnostic test",
    InterventionType.DIETARY_SUPPLEMENT: "Dietary supplement",
    InterventionType.DRUG: "Drug",
    InterventionType.GENETIC: "Genetic",
    InterventionType.PROCEDURE: "Procedure",
    InterventionType.RADIATION: "Radiation",
    InterventionType.OTHER: "Other (registry value)",
}

# Phase buckets where phase does not apply: the registry's NA phase, and phase-less
# observational / expanded-access studies.
NOT_APPLICABLE_PHASE_KEYS = frozenset({PHASE_LABELS[Phase.NA], PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY})

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


class EstimateKind(StrEnum):
    """Which value of a dimension can be an estimate rather than a fact.

    - date: the start date (ACTUAL / ESTIMATED / untyped legacy dates).
    - enrollment: the enrollment count (ACTUAL / ESTIMATED).
    A dimension without one reports estimated counts as null, not 0 (invariant 4).
    """

    DATE = "date"
    ENROLLMENT = "enrollment"


class BucketOrder(StrEnum):
    """How buckets are sorted. The Unknown bucket is always last."""

    BY_COUNT = "by_count"
    BY_KEY = "by_key"
    BY_RANK = "by_rank"


type PathFor = Callable[[int | None], str]


# Phase sets that occur in the whole registry (2026-10-18): each single phase, PHASE1+PHASE2
# (17,142 trials) and PHASE2+PHASE3 (7,623). The other 13 pairs match 0 trials and no trial
# lists more than two phases (docs/api_findings.md §4). A new combination is not counted by
# any bucket, so it shows up as a nonzero remainder and the cohort is fetched instead.
COUNTABLE_PHASE_SETS: tuple[tuple[Phase, ...], ...] = (
    (Phase.EARLY_PHASE1,),
    (Phase.PHASE1,),
    (Phase.PHASE1, Phase.PHASE2),
    (Phase.PHASE2,),
    (Phase.PHASE2, Phase.PHASE3),
    (Phase.PHASE3,),
    (Phase.PHASE4,),
    (Phase.NA,),
)
PHASES_BY_BUCKET: dict[str, tuple[Phase, ...]] = {
    phase_bucket(list(phases)): phases for phases in COUNTABLE_PHASE_SETS
}
# Study types whose phase-less records form PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY; the API
# clause and the normalizer rule must select the same records.
PHASE_LESS_STUDY_TYPES_CLAUSE = "AREA[StudyType](OBSERVATIONAL OR EXPANDED_ACCESS)"
# Sorts after Not Applicable (the highest PHASE_RANK) and before Unknown.
PHASE_NOT_APPLICABLE_STUDY_TYPE_RANK = len(PHASE_RANK)
YEAR_KEY = re.compile(r"\d{4}")


@dataclass(frozen=True)
class ApiCount:
    """How the API counts one bucket of a dimension exactly (the count-per-bucket route).

    `api_filter` maps a bucket key to a `filter.advanced` clause matching exactly the
    trials in that bucket, so one countTotal request gives the bucket's count; it raises
    KeyError for a key it cannot express. `missing_filter` matches the Unknown bucket.
    `keys` lists the bucket keys of an enum-valued dimension. A dimension whose keys
    depend on the data (years) leaves `keys` empty and names `year_range_field`, the
    sortable API field whose first and last values bound its years.
    """

    api_filter: Callable[[str], str]
    missing_filter: str
    keys: tuple[str, ...] = ()
    year_range_field: str | None = None


def phase_api_filter(key: str) -> str:
    """Exactly this phase set: every listed phase present and every other phase absent.

    `AREA[Phase]X` matches any element of the phases list, so "Phase 2" alone would also
    match Phase 1/Phase 2 trials (docs/api_findings.md §4). The phase-less
    observational / expanded-access bucket is selected by study type.
    """
    if key == PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY:
        return f"AREA[Phase]MISSING AND {PHASE_LESS_STUDY_TYPES_CLAUSE}"
    phases = PHASES_BY_BUCKET[key]
    present = " AND ".join(f"AREA[Phase]{phase.value}" for phase in phases)
    others = " OR ".join(phase.value for phase in Phase if phase not in phases)
    return f"{present} AND NOT AREA[Phase]({others})"


def enum_api_filter(area: str, values: type[StrEnum]) -> Callable[[str], str]:
    """`AREA[<area>]<key>` for a key that is a value of `values`."""
    allowed = {member.value for member in values}

    def api_filter(key: str) -> str:
        if key not in allowed:
            raise KeyError(key)
        return f"AREA[{area}]{key}"

    return api_filter


def start_year_api_filter(key: str) -> str:
    """Inclusive whole-year range; month-only dates are indexed as the first of the month."""
    if not YEAR_KEY.fullmatch(key):
        raise KeyError(key)
    return f"AREA[StartDate]RANGE[{key}-01-01,{key}-12-31]"


def bucket_filters(
    api_count: ApiCount, *, year_range: tuple[int, int] | None
) -> list[tuple[str, str]]:
    """(bucket key, filter clause) for every bucket to count, the Unknown bucket last.

    Year-keyed dimensions count each year from `year_range` (first, last), inclusive;
    None means no trial has a readable value, so only Unknown is counted.
    """
    keys: list[str] = list(api_count.keys)
    if api_count.year_range_field is not None and year_range is not None:
        first, last = year_range
        keys = [str(year) for year in range(first, last + 1)]
    return [
        *((key, api_count.api_filter(key)) for key in keys),
        (UNKNOWN_BUCKET_KEY, api_count.missing_filter),
    ]


@dataclass(frozen=True)
class DimensionValue:
    """One value a trial has for a dimension, with the citations that prove it."""

    key: str
    label: str
    citations: tuple[Citation, ...]
    estimated: bool = False
    unknown_date_type: bool = False
    is_unknown: bool = False
    is_unrecognized: bool = False
    is_not_applicable: bool = False
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
    temporal dimension's bucket keys. `api_count` is set when the API can count each
    bucket exactly (see ApiCount); such a dimension must be single-valued, so its bucket
    counts add up to the cohort total. Without it the dimension is fetch-only.
    `estimate_kind` names the value that can be estimated (see EstimateKind); None means
    estimated counts do not apply. `plural` is the plural of `label` when adding "s" is
    wrong ("location countries").
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
    api_count: ApiCount | None = None
    unknown_warning: str | None = None
    estimate_kind: EstimateKind | None = None
    plural: str | None = None

    @property
    def plural_label(self) -> str:
        """The label for several values, e.g. '2,755 lead sponsors'."""
        return self.plural or f"{self.label}s"

    @property
    def other_bucket(self) -> bool:
        """True when a top/bottom-N result can show an exact 'Other' bucket.

        Only a single-valued, non-temporal dimension: each trial is in exactly one bucket,
        so the omitted buckets' counts add up to distinct trials; on a time axis an
        "other periods" bar would have no position.
        """
        return not self.can_overlap and self.kind != DimensionKind.TEMPORAL


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


def labeled(record: TrialRecord, code: str, label: str, path: str) -> DimensionValue:
    """A one-citation enum value keyed and cited by its registry code, shown by `label`."""
    return DimensionValue(key=code, label=label, citations=(cite(record, path, code),))


def unrecognized(record: TrialRecord, path: str, value_raw: str | list[str]) -> DimensionValue:
    """The Unrecognized bucket entry for a value outside the known enum, citing it verbatim."""
    return DimensionValue(
        key=UNRECOGNIZED_BUCKET_KEY,
        label=UNRECOGNIZED_BUCKET_KEY,
        citations=(cite(record, path, value_raw),),
        is_unrecognized=True,
    )


def raw_value(value: StrEnum | UnrecognizedValue) -> str:
    """The exact source string of an enum value or an unrecognized one."""
    return value.raw if isinstance(value, UnrecognizedValue) else value.value


def extract_phase(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The trial's single phase bucket, citing the raw phases list."""
    if record.phase_bucket == UNKNOWN_BUCKET_KEY:
        return []
    if record.phase_bucket == PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY:
        return [
            DimensionValue(
                key=record.phase_bucket,
                label=record.phase_bucket,
                citations=(
                    cite(
                        record,
                        record.source_paths["study_type"],
                        raw_value(record.study_type) if record.study_type is not None else None,
                    ),
                ),
                is_not_applicable=True,
                rank=(PHASE_NOT_APPLICABLE_STUDY_TYPE_RANK,),
            )
        ]
    phases_raw = [raw_value(phase) for phase in record.phases]
    if record.phase_bucket == UNRECOGNIZED_BUCKET_KEY:
        return [unrecognized(record, path(None), phases_raw)]
    value = DimensionValue(
        key=record.phase_bucket,
        label=record.phase_bucket,
        citations=(cite(record, path(None), phases_raw),),
        is_not_applicable=record.phase_bucket in NOT_APPLICABLE_PHASE_KEYS,
        rank=tuple(
            sorted(PHASE_RANK[phase] for phase in record.phases if isinstance(phase, Phase))
        ),
    )
    return [value]


def unparsed_date(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """Unknown period for a date string in an unrecognized format, citing that string.

    Such a trial has no position on a time axis, so it goes to Unknown (as a year-only
    date does for months), not to a separate Unrecognized bucket.
    """
    citation = cite(record, path(None), record.start_date_raw)
    return [
        DimensionValue(
            key=UNKNOWN_BUCKET_KEY, label=UNKNOWN_BUCKET_KEY, citations=(citation,), is_unknown=True
        )
    ]


def extract_start_year(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The start year, citing the raw date string; estimated dates are flagged."""
    if record.start_date_precision == "unrecognized":
        return unparsed_date(record, path)
    if record.start_date is None:
        return []
    key = str(record.start_date.year)
    citation = cite(record, path(None), record.start_date_raw)
    return [dated_value(record, key, citation)]


def dated_value(record: TrialRecord, key: str, citation: Citation) -> DimensionValue:
    """A period value flagged with the start date's type (estimated, or no known type).

    An unrecognized type says neither actual nor estimated, like a missing one.
    """
    return DimensionValue(
        key=key,
        label=key,
        citations=(citation,),
        estimated=record.start_date_type == "estimated",
        unknown_date_type=record.start_date_type in ("unknown", "unrecognized"),
    )


def extract_start_month(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The start month; a year-only date has no month and goes to Unknown."""
    if record.start_date_precision == "unrecognized":
        return unparsed_date(record, path)
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
    agency_class = record.lead_sponsor.agency_class
    if isinstance(agency_class, UnrecognizedValue):
        return [unrecognized(record, path(None), agency_class.raw)]
    return [labeled(record, agency_class.value, AGENCY_CLASS_LABELS[agency_class], path(None))]


def extract_lead_sponsor(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The lead sponsor name."""
    if record.lead_sponsor is None:
        return []
    return [single(record, record.lead_sponsor.name, path(None), record.lead_sponsor.name)]


def extract_intervention_type(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """Each intervention type as listed, shown by its display label; types are set per
    trial, not per drug."""
    values = []
    for index, intervention in enumerate(record.interventions):
        intervention_type = intervention.intervention_type
        if intervention.type_raw is None or intervention_type is None:
            continue
        if isinstance(intervention_type, UnrecognizedValue):
            values.append(unrecognized(record, path(index), intervention.type_raw))
        else:
            label = INTERVENTION_TYPE_LABELS[intervention_type]
            values.append(labeled(record, intervention.type_raw, label, path(index)))
    return values


def extract_overall_status(record: TrialRecord, path: PathFor) -> list[DimensionValue]:
    """The overall status (the API's UNKNOWN is a real value)."""
    if record.overall_status is None:
        return []
    if isinstance(record.overall_status, UnrecognizedValue):
        return [unrecognized(record, path(None), record.overall_status.raw)]
    status = record.overall_status
    return [labeled(record, status.value, OVERALL_STATUS_LABELS[status], path(None))]


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
    """The enrollment bin, citing the raw count; estimated counts are flagged.

    A negative or fractional count has no bin, so it goes to Unrecognized.
    """
    if record.enrollment is None:
        return []
    if isinstance(record.enrollment, UnrecognizedValue):
        return [unrecognized(record, path(None), record.enrollment.raw)]
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
            "trials such as 'Phase 1/Phase 2' form their own bucket, and observational or "
            "expanded-access studies without a phase form another. For 'by phase' or "
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
        api_count=ApiCount(
            api_filter=phase_api_filter,
            missing_filter=f"AREA[Phase]MISSING AND NOT {PHASE_LESS_STUDY_TYPES_CLAUSE}",
            keys=(*PHASES_BY_BUCKET, PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY),
        ),
        unknown_warning=(
            "{unknown} of {total} studies are interventional (or have no recognized study "
            f"type) and list no phase; they are counted in the '{UNKNOWN_BUCKET_KEY}' bucket. "
            "Observational and expanded-access studies without a phase are in "
            f"'{PHASE_NOT_APPLICABLE_STUDY_TYPE_KEY}'."
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
        estimate_kind=EstimateKind.DATE,
        api_count=ApiCount(
            api_filter=start_year_api_filter,
            missing_filter="AREA[StartDate]MISSING",
            year_range_field="StartDate",
        ),
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
        estimate_kind=EstimateKind.DATE,
    ),
    Dimension.COUNTRY: DimensionSpec(
        label="location country",
        title_label="location country",
        plural="location countries",
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
        api_count=ApiCount(
            api_filter=enum_api_filter("LeadSponsorClass", AgencyClass),
            missing_filter="AREA[LeadSponsorClass]MISSING",
            keys=tuple(agency_class.value for agency_class in AgencyClass),
        ),
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
        api_count=ApiCount(
            # Same counts as filter.overallStatus (verified 2026-10-18), and an AREA clause
            # AND-joins with a cohort's own status filter.
            api_filter=enum_api_filter("OverallStatus", OverallStatus),
            missing_filter="AREA[OverallStatus]MISSING",
            keys=tuple(status.value for status in OverallStatus),
        ),
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
        estimate_kind=EstimateKind.ENROLLMENT,
    ),
}


@dataclass(frozen=True)
class RelationSpec:
    """Declaration of one relation: two dimensions whose values become linked nodes.

    `label` names the relation in prose; `title` is the chart title template, with
    `{cohort}` replaced by the cohort label; `description` is rendered into the planner
    prompt. `warnings` are stated on every response for the relation, because they
    qualify what an edge means rather than any particular data.

    When both sides are the same dimension, edges are the unordered pairs of distinct
    values within a trial; otherwise every left value links to every right value.
    `node_filters` maps a node type to the rule that decides which values become nodes.
    """

    label: str
    title: str
    left: Dimension
    left_type: NodeType
    right: Dimension
    right_type: NodeType
    description: str
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    node_filters: dict[NodeType, MeshNodeFilter] = field(default_factory=dict)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def same_dimension(self) -> bool:
        """True when edges link values of one dimension to each other (co-occurrence)."""
        return self.left == self.right


RELATIONS: dict[Relation, RelationSpec] = {
    Relation.SPONSOR_DRUG: RelationSpec(
        label="lead sponsor-drug",
        title="Lead sponsor-drug network for {cohort}",
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
            DRUG_NODE_NOTE,
            NODE_TYPE_NOTE,
        ),
        node_filters={NodeType.DRUG: DRUG_NODE_FILTER},
    ),
    Relation.DRUG_DRUG_COOCCURRENCE: RelationSpec(
        label="drug co-occurrence",
        title=(
            "Drugs listed in the same trial record as {cohort} "
            "(any arm; co-listing is not co-administration)"
        ),
        description=(
            "Network of drugs listed in the same trial record (any arm, including "
            "comparators). For 'which drugs appear in trials together with X'."
        ),
        left=Dimension.INTERVENTION_MESH,
        left_type=NodeType.DRUG,
        right=Dimension.INTERVENTION_MESH,
        right_type=NodeType.DRUG,
        assumptions=(DRUG_NODE_NOTE, NODE_TYPE_NOTE),
        node_filters={NodeType.DRUG: DRUG_NODE_FILTER},
        warnings=(
            "An edge means both drugs are listed in the same trial record, not that they "
            "are co-administered: comparator arms, either-or options (e.g. cisplatin or "
            "carboplatin), separate arms and observational studies are included.",
        ),
    ),
    Relation.CONDITION_DRUG: RelationSpec(
        label="condition-drug",
        title="Condition-drug network for {cohort}",
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
            DRUG_NODE_NOTE,
            NODE_TYPE_NOTE,
        ),
        node_filters={NodeType.DRUG: DRUG_NODE_FILTER},
    ),
}


def distinct_values(record: TrialRecord, spec: DimensionSpec) -> list[DimensionValue]:
    """A trial's values for a dimension, keeping the first occurrence of each key."""
    seen: dict[str, DimensionValue] = {}
    for value in spec.extract(record, path_for(record, spec)):
        seen.setdefault(value.key, value)
    return list(seen.values())
