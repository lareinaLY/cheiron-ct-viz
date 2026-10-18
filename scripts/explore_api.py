"""Exploration spike against the real ClinicalTrials.gov API v2.

Verifies query/filter syntax, field paths, enums, data quirks and limits that the
retrieval and normalize stages will depend on. Prints a report to stdout and writes
small raw fixtures to tests/fixtures/. Findings are summarized in docs/api_findings.md.

Run: uv run python scripts/explore_api.py
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://clinicaltrials.gov/api/v2"
SPEC_URL = "https://clinicaltrials.gov/api/oas/v2"
REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "docs" / "ctgov_openapi.yaml"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# Observed: ~10 back-to-back requests succeed, then HTTP 429 with no Retry-After header.
# 1 request / 1.2 s was sustained for 60 requests without a 429.
MIN_REQUEST_INTERVAL_SECONDS = 1.3
MAX_RETRIES_ON_429 = 6

# Projection covering every field the schema needs for the pembrolizumab sample.
SAMPLE_FIELDS = [
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "StudyType",
    "Phase",
    "StartDate",
    "StartDateType",
    "Condition",
    "InterventionName",
    "InterventionType",
    "InterventionOtherName",
    "LeadSponsorName",
    "LeadSponsorClass",
    "CollaboratorName",
    "CollaboratorClass",
    "LocationCountry",
    "EnrollmentCount",
    "EnrollmentType",
    "ConditionMeshTerm",
    "InterventionMeshTerm",
]

PEMBROLIZUMAB_ALIASES = ("pembrolizumab", "keytruda", "mk-3475", "mk3475", "mk 3475")

logger = logging.getLogger("explore_api")


class PacedClient:
    """Thin httpx wrapper that spaces requests and retries HTTP 429 with backoff.

    The API rate-limits bursts without sending Retry-After, so pacing is client-side.
    """

    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=60.0)
        self._last_request_at = 0.0
        self.request_count = 0
        self.rate_limited_count = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with pacing; returns the final response (which may be a non-429 error)."""
        backoff_seconds = 2.0
        for _ in range(MAX_RETRIES_ON_429 + 1):
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            self.request_count += 1
            response = self._client.get(path, params=params)
            if response.status_code != 429:
                return response
            self.rate_limited_count += 1
            logger.warning("HTTP 429 on %s; backing off %.1fs", path, backoff_seconds)
            time.sleep(backoff_seconds)
            backoff_seconds *= 2
        raise RuntimeError(f"Still rate-limited after {MAX_RETRIES_ON_429} retries: {path}")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET and decode JSON, raising on any non-2xx status."""
        response = self.get(path, params)
        response.raise_for_status()
        return response.json()

    def total_count(self, params: dict[str, Any]) -> int | str:
        """Return totalCount for a /studies query, or the error text on a 4xx."""
        response = self.get(
            "/studies", {**params, "pageSize": 1, "countTotal": "true", "fields": "NCTId"}
        )
        if response.status_code != 200:
            return f"HTTP {response.status_code}: {response.text.strip()[:160]}"
        return response.json()["totalCount"]

    def fetch_all(
        self, params: dict[str, Any], page_size: int = 1000
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch every page of a /studies query; returns (studies, totalCount, pages)."""
        studies: list[dict[str, Any]] = []
        page_params: dict[str, Any] = {**params, "pageSize": page_size, "countTotal": "true"}
        total = -1
        pages = 0
        while True:
            page = self.get_json("/studies", page_params)
            pages += 1
            if pages == 1:
                total = page["totalCount"]
            studies.extend(page["studies"])
            token = page.get("nextPageToken")
            if not token:
                return studies, total, pages
            page_params = {**params, "pageSize": page_size, "pageToken": token}


def section(title: str) -> None:
    """Print a section header in the report."""
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(label: str, value: Any) -> None:
    """Print one labelled finding, JSON-encoding structured values."""
    if isinstance(value, dict | list):
        value = json.dumps(value, ensure_ascii=False)
    print(f"- {label}: {value}")


def dig(record: dict[str, Any], path: str) -> Any:
    """Follow a dotted path through nested dicts; returns None where a key is missing."""
    current: Any = record
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def countries_of(study: dict[str, Any]) -> list[str | None]:
    """Raw country values of all locations of a study (None where country is absent)."""
    locations = dig(study, "protocolSection.contactsLocationsModule.locations") or []
    return [location.get("country") for location in locations]


def intervention_names(study: dict[str, Any]) -> list[str]:
    """Intervention names plus otherNames, as raw strings."""
    names: list[str] = []
    for intervention in dig(study, "protocolSection.armsInterventionsModule.interventions") or []:
        names.append(intervention.get("name", ""))
        names.extend(intervention.get("otherNames", []))
    return names


def intervention_mesh_terms(study: dict[str, Any]) -> list[str]:
    """Normalized intervention MeSH terms from derivedSection."""
    meshes = dig(study, "derivedSection.interventionBrowseModule.meshes") or []
    return [mesh["term"] for mesh in meshes]


def save_fixture(name: str, params: dict[str, Any], payload: Any) -> None:
    """Write a raw API response with the request that produced it."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    document = {
        "_request": {"url": f"{BASE_URL}/studies", "params": params},
        "_captured_on": date.today().isoformat(),
        "response": payload,
    }
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    show("fixture written", f"{path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")


def explore_spec() -> None:
    """1. Download the official OpenAPI spec and report its version and paths."""
    section("1. OpenAPI spec")
    response = httpx.get(SPEC_URL, timeout=60.0)
    response.raise_for_status()
    SPEC_PATH.write_text(response.text)
    show("source", SPEC_URL)
    show("content-type", response.headers.get("content-type"))
    show("saved", f"{SPEC_PATH.relative_to(REPO_ROOT)} ({len(response.content)} bytes)")
    version_lines = [line.strip() for line in response.text.splitlines() if "version:" in line]
    show("spec version line", version_lines[:2])
    paths = [line.strip().rstrip(":") for line in response.text.splitlines() if line[:3] == "  /"]
    show("paths", paths)
    show("mentions 'rate limit'", "rate limit" in response.text.lower())


def explore_version(client: PacedClient) -> None:
    """2. /version: API version and data timestamp."""
    section("2. /version")
    payload = client.get_json("/version")
    show("apiVersion", payload["apiVersion"])
    show("dataTimestamp", payload["dataTimestamp"])


def explore_enums(client: PacedClient) -> None:
    """3. /studies/enums for the enum types the schema will mirror."""
    section("3. /studies/enums")
    wanted = {"Phase", "Status", "StudyType", "InterventionType", "AgencyClass", "DateType"}
    for enum in client.get_json("/studies/enums"):
        if enum["type"] in wanted:
            show(f"{enum['type']} (pieces={enum['pieces']})", [v["value"] for v in enum["values"]])


def explore_search_areas(client: PacedClient) -> None:
    """4. Which pieces each query.* parameter actually searches."""
    section("4. /studies/search-areas")
    wanted = {"cond", "intr", "spons", "locn", "term"}
    free_text_types = {"markup"}
    for document in client.get_json("/studies/search-areas"):
        for area in document["areas"]:
            if area.get("param") not in wanted:
                continue
            pieces = [
                f"{piece}({part['type']},w={part['weight']})"
                for part in area["parts"]
                for piece in part["pieces"]
            ]
            show(f"query.{area['param']} -> {area['name']} ({len(pieces)} pieces)", pieces)
            markup = [
                piece
                for part in area["parts"]
                if part["type"] in free_text_types
                for piece in part["pieces"]
            ]
            show(f"query.{area['param']} free-text (markup) pieces", markup)


def explore_sample_and_paths(client: PacedClient) -> None:
    """5. Sample pembrolizumab studies: totalCount and JSON paths of key fields."""
    section("5. GET /studies query.intr=pembrolizumab pageSize=5 countTotal=true")
    params = {"query.intr": "pembrolizumab", "pageSize": 5, "countTotal": "true"}
    payload = client.get_json("/studies", params)
    show("totalCount", payload["totalCount"])
    show("top-level keys", list(payload))
    paths = {
        "nct_id": "protocolSection.identificationModule.nctId",
        "brief_title": "protocolSection.identificationModule.briefTitle",
        "overall_status": "protocolSection.statusModule.overallStatus",
        "study_type": "protocolSection.designModule.studyType",
        "phases": "protocolSection.designModule.phases",
        "start_date": "protocolSection.statusModule.startDateStruct.date",
        "start_date_type": "protocolSection.statusModule.startDateStruct.type",
        "conditions": "protocolSection.conditionsModule.conditions",
        "interventions": "protocolSection.armsInterventionsModule.interventions",
        "lead_sponsor": "protocolSection.sponsorCollaboratorsModule.leadSponsor",
        "collaborators": "protocolSection.sponsorCollaboratorsModule.collaborators",
        "locations": "protocolSection.contactsLocationsModule.locations",
        "enrollment": "protocolSection.designModule.enrollmentInfo",
    }
    for study in payload["studies"]:
        print()
        for label, path in paths.items():
            value = dig(study, path)
            if label == "interventions" and value:
                value = [{"name": i.get("name"), "type": i.get("type")} for i in value]
            if label == "locations":
                value = sorted({str(country) for country in countries_of(study)})
                label = "location countries (deduped)"
            show(f"{label} [{path}]", value)

    projected_params = {**params, "fields": "|".join(SAMPLE_FIELDS)}
    projected = client.get_json("/studies", projected_params)
    save_fixture("studies_pembrolizumab_page.json", projected_params, projected)


def explore_phases(client: PacedClient) -> None:
    """6. Multi-phase representation and Phase filter semantics."""
    section("6. Multi-phase trials")
    base = {"query.intr": "pembrolizumab"}
    expressions = [
        "AREA[Phase]PHASE2",
        "AREA[Phase]PHASE1",
        "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2",
        "AREA[Phase]PHASE2 AND NOT AREA[Phase]PHASE1",
        "AREA[Phase](PHASE2 OR PHASE3)",
        "AREA[Phase]EARLY_PHASE1",
        "AREA[Phase]NA",
        "AREA[Phase]MISSING",
        "AREA[Phase]PHASE5",
    ]
    for expression in expressions:
        show(
            f"query.intr=pembrolizumab & filter.advanced={expression!r}",
            client.total_count({**base, "filter.advanced": expression}),
        )

    params = {
        **base,
        "filter.advanced": "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2",
        "pageSize": 3,
        "fields": "|".join(SAMPLE_FIELDS),
    }
    payload = client.get_json("/studies", params)
    for study in payload["studies"]:
        show(
            dig(study, "protocolSection.identificationModule.nctId"),
            {"phases": dig(study, "protocolSection.designModule.phases")},
        )
    save_fixture("studies_multi_phase.json", params, payload)


def explore_dates(client: PacedClient) -> None:
    """7. Date range filter syntax."""
    section("7. Date filters")
    base = {"query.intr": "pembrolizumab"}
    expressions = [
        "AREA[StartDate]RANGE[2020-01-01,2020-12-31]",
        "AREA[StartDate]RANGE[2020-01,2020-12]",
        "AREA[StartDate]RANGE[2020,2020]",
        "AREA[StartDate]2020",
        "AREA[StartDate]RANGE[2020-01-01,MAX]",
        "AREA[StartDate]RANGE[MIN,2019-12-31]",
        "AREA[StartDate]MISSING",
        "AREA[StartDateType]ESTIMATED",
        "AREA[StartDateType]ACTUAL",
        f"AREA[StartDate]RANGE[{date.today().isoformat()},MAX]",
    ]
    for expression in expressions:
        show(
            f"filter.advanced={expression!r}",
            client.total_count({**base, "filter.advanced": expression}),
        )


def explore_pagination_and_full_fetch(client: PacedClient) -> list[dict[str, Any]]:
    """8. Pagination behaviour and totalCount reconciliation; returns the full set."""
    section("8. Pagination")
    oversized = client.get_json(
        "/studies", {"query.intr": "pembrolizumab", "pageSize": 5000, "fields": "NCTId"}
    )
    show("pageSize=5000 -> studies returned", len(oversized["studies"]))
    token = oversized["nextPageToken"]
    show("nextPageToken sample", token)
    second_page = client.get(
        "/studies",
        {
            "query.intr": "pembrolizumab",
            "pageSize": 10,
            "pageToken": token,
            "fields": "NCTId",
            "countTotal": "true",
        },
    )
    show("2nd page with countTotal=true contains totalCount", "totalCount" in second_page.json())
    mismatched = client.get(
        "/studies",
        {"query.intr": "nivolumab", "pageSize": 10, "pageToken": token, "fields": "NCTId"},
    )
    show("token reused with a different query -> status", mismatched.status_code)
    show("  body", mismatched.text.strip()[:200])
    garbage = client.get("/studies", {"query.intr": "pembrolizumab", "pageToken": "garbage"})
    show("garbage pageToken -> status", f"{garbage.status_code} {garbage.text.strip()[:120]}")

    started = time.monotonic()
    studies, total, pages = client.fetch_all(
        {"query.intr": "pembrolizumab", "fields": "|".join(SAMPLE_FIELDS)}
    )
    nct_ids = [dig(s, "protocolSection.identificationModule.nctId") for s in studies]
    show("full fetch: totalCount", total)
    show("full fetch: records retrieved", len(studies))
    show("full fetch: unique nct_ids", len(set(nct_ids)))
    show("full fetch: pages / seconds", f"{pages} / {time.monotonic() - started:.1f}")
    show("reconciles", total == len(studies) == len(set(nct_ids)))
    return studies


def explore_fields_projection(client: PacedClient) -> None:
    """9. fields projection syntax and payload reduction."""
    section("9. fields projection")
    base = {"query.intr": "pembrolizumab", "pageSize": 100}
    full = client.get("/studies", base)
    projected = client.get("/studies", {**base, "fields": "|".join(SAMPLE_FIELDS)})
    by_module = client.get(
        "/studies",
        {**base, "fields": "IdentificationModule|DesignModule|ConditionBrowseModule"},
    )
    full_size, projected_size = len(full.content), len(projected.content)
    show("100 studies, no projection (bytes, decoded)", full_size)
    show("100 studies, SAMPLE_FIELDS projection (bytes)", projected_size)
    show("reduction", f"{100 * (1 - projected_size / full_size):.1f}%")
    show(
        "module-name projection status / bytes",
        f"{by_module.status_code} / {len(by_module.content)}",
    )
    show(
        "comma-separated also accepted",
        client.get("/studies", {**base, "pageSize": 1, "fields": "NCTId,Phase"}).status_code,
    )
    show(
        "dotted path accepted",
        client.get(
            "/studies", {**base, "pageSize": 1, "fields": "protocolSection.designModule.phases"}
        ).status_code,
    )
    unknown = client.get("/studies", {**base, "pageSize": 1, "fields": "NotAField"})
    show("unknown field", f"{unknown.status_code} {unknown.text.strip()[:120]}")
    show(
        "projected study keys", json.dumps(projected.json()["studies"][0], ensure_ascii=False)[:400]
    )


def explore_mesh(client: PacedClient, studies: list[dict[str, Any]]) -> None:
    """10. derivedSection MeSH normalization and drug-alias mapping."""
    section("10. derivedSection MeSH terms")
    for alias in ("pembrolizumab", "Keytruda", "MK-3475", "MK3475", "lambrolizumab"):
        show(f"query.intr={alias} totalCount", client.total_count({"query.intr": alias}))
    for expression in (
        "AREA[InterventionName]pembrolizumab",
        "AREA[InterventionName]Keytruda",
        "AREA[InterventionName]MK-3475",
        "AREA[InterventionMeshTerm]pembrolizumab",
        "AREA[InterventionMeshTerm]Keytruda",
        "AREA[InterventionName]EXPANSION[None]pembrolizumab",
        "AREA[InterventionName]EXPANSION[Term]pembrolizumab",
        "AREA[InterventionName]EXPANSION[Concept]pembrolizumab",
        "AREA[InterventionName]EXPANSION[None]Keytruda",
    ):
        show(f"filter.advanced={expression!r}", client.total_count({"filter.advanced": expression}))

    with_mesh = [s for s in studies if "pembrolizumab" in intervention_mesh_terms(s)]
    show(
        "full pembrolizumab set: studies with intervention MeSH 'pembrolizumab'",
        f"{len(with_mesh)} / {len(studies)}",
    )

    alias_to_mesh: dict[str, Counter[str]] = {}
    for study in studies:
        mesh = intervention_mesh_terms(study)
        for name in intervention_names(study):
            lowered = name.lower()
            for alias in ("keytruda", "mk-3475", "mk3475"):
                if alias in lowered and "pembrolizumab" not in lowered:
                    alias_to_mesh.setdefault(name, Counter()).update(
                        ["pembrolizumab" if "pembrolizumab" in mesh else "NO pembrolizumab MeSH"]
                    )
    show(
        "intervention names using only an alias -> mapped MeSH",
        {name: dict(counts) for name, counts in list(alias_to_mesh.items())[:10]},
    )

    raw_variants = Counter(
        name
        for study in studies
        for name in intervention_names(study)
        if any(alias in name.lower() for alias in PEMBROLIZUMAB_ALIASES)
    )
    show("distinct raw intervention-name spellings matching an alias", len(raw_variants))
    show("top raw spellings", raw_variants.most_common(12))

    text_only = [
        s
        for s in studies
        if not any(a in n.lower() for n in intervention_names(s) for a in PEMBROLIZUMAB_ALIASES)
        and "pembrolizumab" not in intervention_mesh_terms(s)
    ]
    show(
        "studies matched by query.intr with no pembrolizumab name/otherName/MeSH",
        f"{len(text_only)} / {len(studies)}",
    )
    show(
        "examples",
        [
            {
                "nct_id": dig(s, "protocolSection.identificationModule.nctId"),
                "interventions": intervention_names(s)[:4],
            }
            for s in text_only[:3]
        ],
    )

    condition_mesh = Counter(
        mesh["term"]
        for study in studies
        for mesh in dig(study, "derivedSection.conditionBrowseModule.meshes") or []
    )
    missing_condition_mesh = sum(
        1 for s in studies if not dig(s, "derivedSection.conditionBrowseModule.meshes")
    )
    show("top condition MeSH terms", condition_mesh.most_common(8))
    spurious_term = "Parkinson Disease 4, Autosomal Dominant Lewy Body"
    spurious = [
        {
            "nct_id": dig(s, "protocolSection.identificationModule.nctId"),
            "conditions": dig(s, "protocolSection.conditionsModule.conditions"),
        }
        for s in studies
        if spurious_term
        in [m["term"] for m in dig(s, "derivedSection.conditionBrowseModule.meshes") or []]
    ]
    show(f"studies with condition MeSH {spurious_term!r}", len(spurious))
    show("  their raw conditions (examples)", spurious[:3])
    show("studies without any condition MeSH term", f"{missing_condition_mesh} / {len(studies)}")


def explore_locations(client: PacedClient, studies: list[dict[str, Any]]) -> None:
    """11. Location country data quality over the full pembrolizumab set."""
    section("11. Locations")
    no_locations = [s for s in studies if not countries_of(s)]
    missing_country = sum(1 for s in studies for c in countries_of(s) if not c)
    distinct = Counter(c for s in studies for c in set(countries_of(s)) if c)
    show("studies with no locations at all", f"{len(no_locations)} / {len(studies)}")
    show("location rows with missing country", missing_country)
    show("distinct country strings", len(distinct))
    show("top countries (studies per country)", distinct.most_common(10))
    suspicious = [
        c
        for c in distinct
        if c != c.strip() or c.isupper() or "," in c or any(ch.isdigit() for ch in c)
    ]
    show("suspicious spellings (whitespace/upper/comma/digit)", suspicious)
    show("all distinct countries (sorted)", sorted(distinct))
    multi_country = max(studies, key=lambda s: len(set(countries_of(s))))
    show(
        "most multi-country study",
        {
            "nct_id": dig(multi_country, "protocolSection.identificationModule.nctId"),
            "countries": len(set(countries_of(multi_country))),
            "location_rows": len(countries_of(multi_country)),
        },
    )
    show("location rows per study (max)", max(len(countries_of(s)) for s in studies))

    global_values = client.get_json("/stats/field/values", {"fields": "LocationCountry"})
    entry = global_values[0]
    show("global LocationCountry uniqueValuesCount", entry.get("uniqueValuesCount"))
    show("global LocationCountry missingStudiesCount", entry.get("missingStudiesCount"))
    top_values = entry.get("topValues", [])
    show("global LocationCountry top values", top_values[:8])
    show("global LocationCountry values listed", len(top_values))
    show(
        "global values with parentheses/commas (naming quirks)",
        [v["value"] for v in top_values if "(" in v["value"] or "," in v["value"]],
    )

    # Smallest study spanning several countries keeps the fixture small but representative.
    fixture_study = min(
        (s for s in studies if len(set(countries_of(s))) >= 5),
        key=lambda s: len(countries_of(s)),
    )
    nct_id = dig(fixture_study, "protocolSection.identificationModule.nctId")
    show(
        "multi-country fixture study",
        {
            "nct_id": nct_id,
            "countries": sorted(set(countries_of(fixture_study))),
            "location_rows": len(countries_of(fixture_study)),
        },
    )
    params = {
        "filter.ids": nct_id,
        "fields": "|".join(SAMPLE_FIELDS),
        "pageSize": 1,
    }
    save_fixture("studies_multi_country.json", params, client.get_json("/studies", params))


def explore_field_stats(client: PacedClient) -> None:
    """12. /stats/field/values output and whether query/filter params are honoured."""
    section("12. /stats/field/values")
    unfiltered = client.get_json("/stats/field/values", {"fields": "Phase"})
    show("fields=Phase response", unfiltered)
    for extra in ({"query.intr": "pembrolizumab"}, {"filter.advanced": "AREA[Phase]PHASE1"}):
        response = client.get("/stats/field/values", {"fields": "Phase", **extra})
        show(f"with {extra} -> status", f"{response.status_code} {response.text.strip()[:120]}")
    sizes = client.get_json("/stats/field/sizes", {"fields": "Phase"})
    show("/stats/field/sizes (list-size distribution)", sizes)


def explore_rate_limit_headers(client: PacedClient) -> None:
    """13. Rate-limit related response headers."""
    section("13. Rate limiting")
    response = client.get("/studies", {"pageSize": 1, "fields": "NCTId"})
    rate_headers = {
        key: value
        for key, value in response.headers.items()
        if "rate" in key.lower() or "limit" in key.lower() or "retry" in key.lower()
    }
    show("rate-limit headers on a 200", rate_headers or "none")
    show("429s encountered during this run (paced client)", client.rate_limited_count)
    show("requests made during this run", client.request_count)


def explore_sizing(client: PacedClient) -> None:
    """14. totalCount for representative queries."""
    section("14. Sizing")
    queries = [
        {"query.intr": "pembrolizumab"},
        {"query.cond": "breast cancer"},
        {"query.cond": "non-small cell lung cancer"},
        {"query.cond": "diabetes"},
        {},
    ]
    for params in queries:
        show(json.dumps(params) if params else "all studies", client.total_count(params))


def explore_date_shapes(studies: list[dict[str, Any]]) -> None:
    """7b. Shapes of start dates in the full pembrolizumab set."""
    section("7b. Start date shapes (full pembrolizumab set)")
    today = date.today().isoformat()
    shapes: Counter[str] = Counter()
    types: Counter[str] = Counter()
    future_by_type: Counter[str] = Counter()
    past_estimated = 0
    examples: dict[str, str] = {}
    for study in studies:
        struct = dig(study, "protocolSection.statusModule.startDateStruct") or {}
        value = struct.get("date")
        date_type = struct.get("type", "<absent>")
        types[date_type] += 1
        if value is None:
            shapes["<missing>"] += 1
            continue
        shape = {4: "YYYY", 7: "YYYY-MM", 10: "YYYY-MM-DD"}.get(len(value), f"other:{value}")
        shapes[shape] += 1
        examples.setdefault(
            shape, f"{dig(study, 'protocolSection.identificationModule.nctId')} {value} {date_type}"
        )
        if value > today:
            future_by_type[date_type] += 1
        elif date_type == "ESTIMATED":
            past_estimated += 1
    show("date shapes", dict(shapes))
    show("examples", examples)
    show("startDateStruct.type values", dict(types))
    show("future start dates by type", dict(future_by_type))
    show("ESTIMATED start dates already in the past", past_estimated)

    phases = Counter(
        tuple(dig(s, "protocolSection.designModule.phases") or ["<missing>"]) for s in studies
    )
    section("6b. Phase combinations (full pembrolizumab set)")
    show("phase lists", {"|".join(k): v for k, v in phases.most_common()})
    study_types = Counter(dig(s, "protocolSection.designModule.studyType") for s in studies)
    show("study types", dict(study_types))
    enrollment_missing = sum(
        1 for s in studies if dig(s, "protocolSection.designModule.enrollmentInfo.count") is None
    )
    show("studies with no enrollment count", enrollment_missing)


def explore_partial_date_ranges(client: PacedClient, studies: list[dict[str, Any]]) -> None:
    """7c. How a YYYY-MM start date is matched by day-precision RANGE filters."""
    section("7c. Partial-date range semantics")
    study = next(
        s
        for s in studies
        if len(dig(s, "protocolSection.statusModule.startDateStruct.date") or "") == 7
    )
    nct_id = dig(study, "protocolSection.identificationModule.nctId")
    month = dig(study, "protocolSection.statusModule.startDateStruct.date")
    show("probe study", f"{nct_id} startDate={month}")
    year, month_number = (int(part) for part in month.split("-"))
    next_month = f"{year + month_number // 12}-{month_number % 12 + 1:02d}"
    for expression in (
        f"AREA[StartDate]{month}",
        f"AREA[StartDate]RANGE[{month}-01,{month}-01]",
        f"AREA[StartDate]RANGE[{month}-02,{month}-28]",
        f"AREA[StartDate]RANGE[{month}-15,{next_month}-15]",
    ):
        show(
            f"filter.ids={nct_id} & filter.advanced={expression!r}",
            client.total_count({"filter.ids": nct_id, "filter.advanced": expression}),
        )


def main() -> None:
    """Run every exploration section in order."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    client = PacedClient(BASE_URL)
    explore_spec()
    explore_version(client)
    explore_enums(client)
    explore_search_areas(client)
    explore_sample_and_paths(client)
    explore_phases(client)
    explore_dates(client)
    studies = explore_pagination_and_full_fetch(client)
    explore_date_shapes(studies)
    explore_partial_date_ranges(client, studies)
    explore_fields_projection(client)
    explore_mesh(client, studies)
    explore_locations(client, studies)
    explore_field_stats(client)
    explore_sizing(client)
    explore_rate_limit_headers(client)


if __name__ == "__main__":
    main()
