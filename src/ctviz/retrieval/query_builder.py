"""QueryPlan filters -> ClinicalTrials.gov /studies params. Pure functions, no I/O.

Free-text values go to the API's `query.*` term search unchanged, because its synonym
expansion is the reason to use it (NSCLC: 8601 hits raw vs 8078 as a quoted phrase).
Any Essie syntax in such a value is rejected instead, so neither a user nor the LLM can
change search semantics by accident. The drug name in `intervention_name` mode is
always quoted: unquoted, "pembrolizumab (KEYTRUDA®)" is parsed as grouping (559 hits
instead of 43). See docs/api_findings.md §14.

`intervention_name` mode matches the intervention name OR the derived intervention MeSH
term, because each misses trials the other finds: the API does not expand every code name
(28 nivolumab trials are listed only as ONO-4538 / BMS-936558 and reachable only via MeSH),
and combination strings like "Pembrolizumab+Lenvatinib" have no MeSH term (72 pembrolizumab
trials). See docs/api_findings.md §16.
"""

from __future__ import annotations

import re

from ctviz.errors import InvalidFilterError
from ctviz.schemas.plan import Filters, MatchMode

# Characters with syntactic meaning in Essie expressions.
ESSIE_SYNTAX_CHARACTERS = frozenset('"()[]')
# Essie operators are case-sensitive: only the uppercase words act as operators.
ESSIE_OPERATOR = re.compile(r"(?<!\S)(AND|OR|NOT)(?!\S)")

EXCLUDE_WITHDRAWN_CLAUSE = "NOT AREA[OverallStatus]WITHDRAWN"

# Only date and numeric fields can be sorted on (NCTId cannot break ties), at most 2 keys.
RETRIEVAL_SORT_FIELD = "StudyFirstPostDate"
RETRIEVAL_SORT_DIRECTION = "desc"
RETRIEVAL_SORT = f"{RETRIEVAL_SORT_FIELD}:{RETRIEVAL_SORT_DIRECTION}"

FREE_TEXT_PARAMS: dict[str, str] = {
    "condition": "query.cond",
    "sponsor": "query.spons",
    "country": "query.locn",
}

SEMANTICS: dict[str, str] = {
    "drug_intervention_name": (
        "Matches the intervention name or derived intervention MeSH term: filter.advanced "
        '(AREA[InterventionName]"<drug>" OR AREA[InterventionMeshTerm]"<drug>") matches the '
        "listed intervention names (with the API's concept expansion of aliases) or the "
        "study-level intervention MeSH terms, not titles or descriptions. Trials whose "
        "intervention is a class placeholder (e.g. 'PD-1 inhibitor') listing the drug as an "
        "other name are included, and so are MeSH mis-mappings."
    ),
    "drug_broad": (
        "Broad search: query.intr matches intervention names and other names, arm labels and "
        "descriptions, titles, keywords and MeSH ancestors, with synonym expansion. Trials "
        "that only mention the drug (e.g. as a comparator) are included."
    ),
    "condition": (
        "query.cond term search with synonym expansion over conditions, titles, condition "
        "MeSH terms and keywords; terms are not matched as an exact phrase."
    ),
    "sponsor": (
        "query.spons term search over lead sponsor, collaborators and organization name; "
        "collaborator-only matches are included."
    ),
    "country": (
        "query.locn term search over location country, state, city and facility; e.g. "
        "'Georgia' also matches the US state."
    ),
    "phases": (
        "AREA[Phase] matches trials listing any of the requested phases, so multi-phase "
        "trials (e.g. Phase 1/Phase 2) are included."
    ),
    "statuses": "filter.overallStatus: overall status equals one of the requested values.",
    "study_types": (
        "AREA[StudyType] matches studies of any of the requested study types "
        "(interventional, observational, expanded access)."
    ),
    "primary_purposes": (
        "AREA[DesignPrimaryPurpose] matches studies whose primary purpose is any of the "
        "requested ones; only interventional studies record a primary purpose, so "
        "observational and expanded-access studies never match."
    ),
    "exclude_withdrawn": (
        "NOT AREA[OverallStatus]WITHDRAWN leaves out studies withdrawn before enrolling "
        "their first participant."
    ),
    "start_year": (
        "AREA[StartDate] inclusive year range; month-only dates count as the first of the "
        "month, and trials with no start date are excluded."
    ),
}


def build_params(filters: Filters, match_mode: MatchMode) -> dict[str, str]:
    """Translate one cohort's filters into /studies query params.

    - drug: `intervention_name` -> `filter.advanced=(AREA[InterventionName]"<drug>" OR
      AREA[InterventionMeshTerm]"<drug>")`; `broad` -> `query.intr=<drug>`.
    - condition / sponsor / country -> `query.cond` / `query.spons` / `query.locn`, raw.
    - phases -> `AREA[Phase]X` or `AREA[Phase](X OR Y)`; statuses -> `filter.overallStatus=A|B`.
    - start years -> `AREA[StartDate]RANGE[<from>-01-01,<to>-12-31]`, MIN/MAX when open.
    - study types -> `AREA[StudyType]X` or `AREA[StudyType](X OR Y)`; primary purposes ->
      `AREA[DesignPrimaryPurpose]...` likewise; exclude_withdrawn ->
      `NOT AREA[OverallStatus]WITHDRAWN` (verified live to partition the registry).
    All `filter.advanced` clauses are joined with AND. Raises InvalidFilterError for blank
    values, Essie syntax in free text, and double quotes or operator words in a quoted
    drug name.
    """
    params: dict[str, str] = {}
    clauses: list[str] = []

    if filters.drug is not None:
        if match_mode == "broad":
            params["query.intr"] = free_text_value("drug", filters.drug)
        else:
            clauses.append(intervention_clause(quotable_value("drug", filters.drug)))
    for field, param in FREE_TEXT_PARAMS.items():
        value = getattr(filters, field)
        if value is not None:
            params[param] = free_text_value(field, value)
    if filters.statuses:
        params["filter.overallStatus"] = "|".join(status.value for status in filters.statuses)
    if filters.phases:
        clauses.append(phase_clause([phase.value for phase in filters.phases]))
    if filters.start_year_from is not None or filters.start_year_to is not None:
        clauses.append(start_year_clause(filters.start_year_from, filters.start_year_to))
    if filters.study_types:
        clauses.append(any_of_clause("StudyType", [t.value for t in filters.study_types]))
    if filters.primary_purposes:
        clauses.append(
            any_of_clause("DesignPrimaryPurpose", [p.value for p in filters.primary_purposes])
        )
    if filters.exclude_withdrawn:
        clauses.append(EXCLUDE_WITHDRAWN_CLAUSE)
    if clauses:
        params["filter.advanced"] = " AND ".join(clauses)
    return params


def study_query_params(filters: Filters, match_mode: MatchMode) -> dict[str, str]:
    """The /studies params for one cohort: its filters plus the pinned retrieval order.

    The API's default order is repeatable but undocumented ("not sorted by default for a
    performance reason"), so a fetch cut by the record cap would be an unexplainable
    subset. Sorting by first-post date makes it "the most recently registered trials",
    which stays stable between runs because that date never changes (docs/api_findings.md §6).
    """
    return {**build_params(filters, match_mode), "sort": RETRIEVAL_SORT}


def with_bucket_filter(params: dict[str, str], clause: str) -> dict[str, str]:
    """The cohort's params narrowed to one bucket: its clause AND-joined in parentheses.

    The parentheses keep a bucket clause's own AND NOT terms together (e.g. "Phase 2 and
    no other phase") whatever the cohort's clauses are.
    """
    existing = params.get("filter.advanced")
    bucket = f"({clause})"
    joined = bucket if existing is None else f"{existing} AND {bucket}"
    return {**params, "filter.advanced": joined}


def search_semantics(filters: Filters, match_mode: MatchMode) -> dict[str, str]:
    """Describe, per filter in use, what the API actually matches (for meta)."""
    semantics: dict[str, str] = {}
    if filters.drug is not None:
        semantics["drug"] = SEMANTICS[f"drug_{match_mode}"]
    for field in (
        "condition",
        "sponsor",
        "country",
        "phases",
        "statuses",
        "study_types",
        "primary_purposes",
        "exclude_withdrawn",
    ):
        if getattr(filters, field):
            semantics[field] = SEMANTICS[field]
    if filters.start_year_from is not None or filters.start_year_to is not None:
        semantics["start_year"] = SEMANTICS["start_year"]
    return semantics


def reject_escapes(field: str, value: str) -> None:
    """Raise InvalidFilterError for a backslash or a C0 control character.

    Whether Essie treats a backslash as an escape is unconfirmed (docs/api_findings.md
    §14): inside a quoted value it could escape the closing quote. Control characters
    (newline, NUL, ...) are never part of a name and their handling is untested.
    """
    if "\\" in value:
        raise InvalidFilterError(field, value, "backslashes (\\) cannot be searched")
    if any(ord(character) < 0x20 for character in value):
        raise InvalidFilterError(
            field, value, "control characters (newline, tab, NUL, ...) cannot be searched"
        )


def non_blank(field: str, value: str) -> str:
    """The trimmed value; a blank value would silently match everything. Values with a
    backslash or a control character left after trimming are rejected (`reject_escapes`)."""
    trimmed = value.strip()
    if not trimmed:
        raise InvalidFilterError(field, value, "value is blank")
    reject_escapes(field, trimmed)
    return trimmed


def free_text_value(field: str, value: str) -> str:
    """A value safe to pass raw to a `query.*` term search."""
    trimmed = non_blank(field, value)
    found = sorted(ESSIE_SYNTAX_CHARACTERS & set(trimmed))
    if found:
        raise InvalidFilterError(
            field,
            value,
            f"contains search syntax characters {' '.join(found)}; remove them so the value "
            "is searched as plain terms",
        )
    reject_operators(field, value, trimmed)
    return trimmed


def quotable_value(field: str, value: str) -> str:
    """A value safe to wrap in double quotes; an embedded quote cannot be escaped.

    Parentheses stay allowed: quoted, they are literal, and real intervention names
    contain them ("pembrolizumab (KEYTRUDA®)"). An operator word is rejected even though
    it would be inert inside the quotes: no intervention name contains one, and a value
    like 'pembrolizumab) OR (nivolumab' is an attempt to search for two drugs that would
    silently match neither.
    """
    trimmed = non_blank(field, value)
    if '"' in trimmed:
        raise InvalidFilterError(field, value, 'double quotes (") cannot be searched')
    reject_operators(field, value, trimmed)
    return trimmed


def reject_operators(field: str, value: str, trimmed: str) -> None:
    """Raise InvalidFilterError if the value contains an uppercase AND, OR or NOT word."""
    operator = ESSIE_OPERATOR.search(trimmed)
    if operator:
        raise InvalidFilterError(
            field,
            value,
            f"contains the search operator {operator.group(1)}; use one value per filter "
            "or lowercase the word",
        )


def intervention_clause(drug: str) -> str:
    """Intervention name OR derived intervention MeSH term, parenthesized for AND-joining.

    Without the parentheses AND binds tighter than OR, so other filters would apply to the
    MeSH branch only (2611 hits instead of 1951 for pembrolizumab in Phase 2/3, §16).
    """
    return f'(AREA[InterventionName]"{drug}" OR AREA[InterventionMeshTerm]"{drug}")'


def phase_clause(phases: list[str]) -> str:
    """AREA[Phase] clause; several phases are OR-combined (any listed phase matches)."""
    return any_of_clause("Phase", phases)


def any_of_clause(area: str, values: list[str]) -> str:
    """AREA[<area>] clause matching any of `values` (enum codes, never free text)."""
    if len(values) == 1:
        return f"AREA[{area}]{values[0]}"
    return f"AREA[{area}]({' OR '.join(values)})"


def start_year_clause(start: int | None, end: int | None) -> str:
    """Inclusive AREA[StartDate] range over whole years (verified syntax, §5)."""
    lower = f"{start:04d}-01-01" if start is not None else "MIN"
    upper = f"{end:04d}-12-31" if end is not None else "MAX"
    return f"AREA[StartDate]RANGE[{lower},{upper}]"
