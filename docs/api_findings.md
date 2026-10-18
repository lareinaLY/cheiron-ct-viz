# ClinicalTrials.gov API v2 — verified findings

## Expectations vs. findings

| Assumption before exploring | What the data showed | Design consequence |
|---|---|---|
| `query.intr=X` returns trials that test drug X | It also matches titles, arm labels, keywords, intervention/arm descriptions and MeSH ancestors, with synonym expansion. 336 / 2981 pembrolizumab hits mention the drug only in text or comparator arms (§2) | Default match mode is `AREA[InterventionName]` (exact). `query.intr` is an opt-in `broad` mode, echoed in `meta.match_mode` |
| Derived MeSH terms are a clean normalization layer | Intervention MeSH reliably merges aliases (Keytruda, MK-3475 → pembrolizumab, C582435). Condition MeSH has spurious terms (201 cancer trials tagged "Parkinson Disease 4…"), and 435 / 2981 studies have none. MeSH is study-level, not tied to an intervention (§8) | Drugs are grouped by study-level intervention MeSH (id + term). Conditions use raw strings. No intervention↔MeSH linking is attempted |
| `ESTIMATED` start dates are future dates | 266 `ESTIMATED` start dates are already in the past, and 55 legacy records have a date but no type (§5) | Estimated dates are counted and flagged (`estimated_count`), not excluded. A missing type is `"unknown"` |
| A trial has one phase | `phases` is a list; 24,765 studies have two. A Phase filter matches any element, so per-phase API counts overlap (§4) | Multi-phase trials get their own mutually exclusive bucket ("Phase 1/Phase 2") |
| One location row per country | Locations are per site, up to 1660 rows for one study, with repeated countries (§9) | Countries are deduplicated within a study. Country buckets overlap across multi-country trials, and the result is flagged `buckets_overlap` |
| `pageSize` can be set as large as needed | It is silently capped at 1000, with no error (§6) | Retrieval pages at 1000 and reconciles the retrieved count against `totalCount` |
| A `pageToken` is tied to its query | The token is not bound to the query; reusing it with other params returns wrong results with HTTP 200 (§6) | Page requests resend frozen params; only `pageToken` changes |
| The API publishes a rate limit or `Retry-After` | It publishes neither. After about 10 rapid requests it returns HTML 429 with no `Retry-After`; 1 request / 1.2 s is sustainable (§11) | Retrieval paces requests itself and retries 429 with exponential backoff |
| `/stats/field/values` can count values within a query | It rejects `query.*` and `filter.*` params with 400 and only covers the whole database (§10) | Subset aggregation is done client-side over fetched records |

## Open ambiguities

These are choices a reasonable person could make differently. Each states our choice and the alternative.

| Question | Our choice | Alternative |
|---|---|---|
| Count unit | Trials (studies), stated in `meta.count_unit` | Sites, arms or enrolled participants |
| Multi-phase bucketing | Own combined bucket ("Phase 1/Phase 2"); buckets are mutually exclusive | Count the trial in every phase it lists (totals exceed the number of trials), or assign it to its highest phase |
| Territories | Reported as the API gives them (Puerto Rico, Guam, Hong Kong are separate) | Roll territories up into a sovereign country |
| Drug matching | Exact `AREA[InterventionName]` by default; `query.intr` as opt-in `broad` | Broad by default, or `AREA[InterventionMeshTerm]` |
| Non-drug nodes in networks | Drug nodes are all intervention MeSH terms, including radiation or procedures; this is noted in meta assumptions | Filter nodes by intervention type (DRUG/BIOLOGICAL). This needs an intervention↔MeSH link the API doesn't provide |
| Trials without intervention MeSH | `Unknown` bucket, with a meta warning giving the count | Fall back to the raw intervention name as the bucket key, which splits aliases into separate buckets |
| "Fewest" questions ("which countries have the fewest trials") | Not a ranking: `ranking_intent` covers most/top/leading only, so the chart is untrimmed and sorted by count, with the smallest values last | Trim to the N smallest buckets, which needs a sort direction in the plan |
| Year-only start date in monthly views | `Unknown` bucket for `start_month` (citing the raw year); counted normally for `start_year` | Place it in January (the API's indexing convention) |

---

Source of truth for params and enums: `docs/ctgov_openapi.yaml`, downloaded from
`https://clinicaltrials.gov/api/oas/v2` (OpenAPI 3.0.3, API version `2.0.5`).
Every number below was produced by `scripts/explore_api.py` against the live API.

- Run date: 2026-10-17. `/version` → `apiVersion: 2.0.5`, `dataTimestamp: 2026-10-16T09:00:05`
  (the data is refreshed daily; counts below drift accordingly).
- Base URL: `https://clinicaltrials.gov/api/v2`
- Fixtures (all captured with a `fields` projection, raw request stored under `_request`):
  - `tests/fixtures/studies_pembrolizumab_page.json`: first page, `query.intr=pembrolizumab`, pageSize=5.
    Contains a PHASE1+PHASE2 trial (NCT06939036), a 20-country trial (NCT03834519), alias-only
    intervention names (`MK-3475`, `pembrolizumab (KEYTRUDA®)`), and `collaborators` both present and absent.
  - `tests/fixtures/studies_multi_phase.json`: 3 trials with `phases: ["PHASE1","PHASE2"]`.
  - `tests/fixtures/studies_multi_country.json`: NCT02129556, with 11 locations in 5 countries.

---

## 1. Enums (`GET /studies/enums`)

| Enum type | Pieces | Values |
|---|---|---|
| `Phase` | Phase | `NA`, `EARLY_PHASE1`, `PHASE1`, `PHASE2`, `PHASE3`, `PHASE4` |
| `Status` | OverallStatus, LastKnownStatus | `ACTIVE_NOT_RECRUITING`, `COMPLETED`, `ENROLLING_BY_INVITATION`, `NOT_YET_RECRUITING`, `RECRUITING`, `SUSPENDED`, `TERMINATED`, `WITHDRAWN`, `AVAILABLE`, `NO_LONGER_AVAILABLE`, `TEMPORARILY_NOT_AVAILABLE`, `APPROVED_FOR_MARKETING`, `WITHHELD`, `UNKNOWN` |
| `StudyType` | StudyType | `EXPANDED_ACCESS`, `INTERVENTIONAL`, `OBSERVATIONAL` |
| `InterventionType` | InterventionType | `BEHAVIORAL`, `BIOLOGICAL`, `COMBINATION_PRODUCT`, `DEVICE`, `DIAGNOSTIC_TEST`, `DIETARY_SUPPLEMENT`, `DRUG`, `GENETIC`, `PROCEDURE`, `RADIATION`, `OTHER` |
| `AgencyClass` | OrgClass, LeadSponsorClass, CollaboratorClass | `NIH`, `FED`, `OTHER_GOV`, `INDIV`, `INDUSTRY`, `NETWORK`, `AMBIG`, `OTHER`, `UNKNOWN` |
| `DateType` | StartDateType, PrimaryCompletionDateType, CompletionDateType, … | `ACTUAL`, `ESTIMATED` |

Notes:
- `Status` mixes trial statuses with expanded-access statuses (`AVAILABLE`, `APPROVED_FOR_MARKETING`, …).
- The `Status` and `AgencyClass` enums both contain an `UNKNOWN` value. It is a real API value
  and must stay separate from our own "missing" bucket.
- Invalid enum values in `filter.advanced` are rejected with HTTP 400, e.g. `AREA[Phase]PHASE5` →
  `Error parsing query in advanced filter: Allowed values for enum field protocolSection.designModule.phases are NA, EARLY_PHASE1, …`.

## 2. Search areas: what `query.*` actually searches (`GET /studies/search-areas`)

| Param | Area | Pieces searched (weight) |
|---|---|---|
| `query.cond` | ConditionSearch | Condition (.95), BriefTitle (.6), OfficialTitle (.55), ConditionMeshTerm (.5), ConditionAncestorTerm (.4), Keyword (.3), NCTId (.2) |
| `query.intr` | InterventionSearch | InterventionName (.95), InterventionType (.85), ArmGroupType (.85), InterventionOtherName (.75), BriefTitle (.65), OfficialTitle (.6), ArmGroupLabel (.5), InterventionMeshTerm (.5), Keyword (.5), InterventionAncestorTerm (.4), **InterventionDescription (.4, markup)**, **ArmGroupDescription (.4, markup)** |
| `query.spons` | SponsorSearch | LeadSponsorName (1.0), CollaboratorName (.9), OrgFullName (.6) |
| `query.locn` | LocationSearch | LocationCity, LocationState, LocationCountry, LocationFacility (.95), LocationZip (.35) |
| `query.term` | BasicSearch | 57 pieces, including titles, conditions, interventions, BriefSummary, outcome descriptions, locations, investigator and contact names |

**Yes, `query.intr` searches free text**: titles, arm labels, keywords, intervention
descriptions and arm-group descriptions, plus MeSH *ancestor* terms. It also applies synonym expansion.
Measured on `query.intr=pembrolizumab` (2981 studies):

- 336 / 2981 studies have no intervention name, otherName or intervention MeSH term mentioning
  pembrolizumab/Keytruda/MK-3475. Examples: NCT05170256 (intervention: "Trastuzumab"),
  NCT04116320 ("Echopulse", "Focused Ultrasound Ablation", "Poly ICLC").
- `filter.advanced=AREA[InterventionName]pembrolizumab` → 2575, `AREA[InterventionMeshTerm]pembrolizumab` → 2570.

For "trials testing drug X", `AREA[InterventionName]X` or `AREA[InterventionMeshTerm]X`
in `filter.advanced` is much tighter than `query.intr=X`.

## 3. Field paths and sample values (`GET /studies?query.intr=pembrolizumab&pageSize=5&countTotal=true`)

`totalCount: 2981`. Response top-level keys: `totalCount`, `studies`, `nextPageToken`.
Each study has `protocolSection`, `derivedSection`, `hasResults`, and, when present,
`resultsSection` and `documentSection`.

| Concept | JSON path | `fields` piece name | Sample value |
|---|---|---|---|
| NCT ID | `protocolSection.identificationModule.nctId` | `NCTId` | `"NCT04993677"` |
| Brief title | `protocolSection.identificationModule.briefTitle` | `BriefTitle` | `"A Study of SEA-CD40 Given With Other Drugs in Cancers"` |
| Overall status | `protocolSection.statusModule.overallStatus` | `OverallStatus` | `"COMPLETED"` |
| Study type | `protocolSection.designModule.studyType` | `StudyType` | `"INTERVENTIONAL"` |
| Phases | `protocolSection.designModule.phases` (**list**) | `Phase` | `["PHASE1","PHASE2"]` (NCT06939036) |
| Start date | `protocolSection.statusModule.startDateStruct.date` | `StartDate` | `"2021-10-06"` or `"2026-09"` |
| Start date type | `protocolSection.statusModule.startDateStruct.type` | `StartDateType` | `"ACTUAL"` / `"ESTIMATED"` |
| Conditions | `protocolSection.conditionsModule.conditions` (list of free text) | `Condition` | `["Melanoma","Carcinoma, Non-Small- Cell Lung"]` |
| Interventions | `protocolSection.armsInterventionsModule.interventions[].{name,type,otherNames}` | `InterventionName`, `InterventionType`, `InterventionOtherName` | `{"name":"pembrolizumab (KEYTRUDA®)","type":"DRUG"}` |
| Lead sponsor | `protocolSection.sponsorCollaboratorsModule.leadSponsor.{name,class}` | `LeadSponsorName`, `LeadSponsorClass` | `{"name":"Seagen Inc.","class":"INDUSTRY"}` |
| Collaborators | `protocolSection.sponsorCollaboratorsModule.collaborators[].{name,class}` (key **absent** when none) | `CollaboratorName`, `CollaboratorClass` | `[{"name":"Merck Sharp & Dohme LLC","class":"INDUSTRY"}]` |
| Location countries | `protocolSection.contactsLocationsModule.locations[].country` (one row per site) | `LocationCountry` | `"United States"` |
| Enrollment | `protocolSection.designModule.enrollmentInfo.{count,type}` | `EnrollmentCount`, `EnrollmentType` | `{"count":77,"type":"ACTUAL"}` |
| Condition MeSH | `derivedSection.conditionBrowseModule.meshes[].{id,term}` | `ConditionMeshTerm` | `{"id":"D008545","term":"Melanoma"}` |
| Intervention MeSH | `derivedSection.interventionBrowseModule.meshes[].{id,term}` | `InterventionMeshTerm` | `{"id":"C582435","term":"pembrolizumab"}` |

Quirks:
- Empty lists are omitted rather than returned as `[]`. Examples: `collaborators` is missing for
  NCT03631407, and `locations` is missing for 213 studies. Treat absent and empty the same way,
  and report both as missing.
- The same drug appears as `DRUG` in one trial and `BIOLOGICAL` in another
  (NCT04993677: DRUG; NCT03631407: BIOLOGICAL). Intervention type is set per trial, not per drug.
- Intervention names are messy free text: 612 distinct raw spellings in the pembrolizumab set,
  e.g. `Pembrolizumab` (1978), `Keytruda` (879), `MK-3475` (718), `KEYTRUDA®` (305),
  `MK 3475` (66), `Pembrolizumab Biosimilar BCD-201` (79). The same trial can also list one
  drug in several arms.

## 4. Phases and multi-phase trials

- `phases` is a list. Multi-phase trials list both phases: `["PHASE1","PHASE2"]` or `["PHASE2","PHASE3"]`.
  Globally (`/stats/field/sizes?fields=Phase`) the list size is 1 or 2, never more:
  438,573 studies have 1 phase and 24,765 have 2.
- Phase combinations in the pembrolizumab set (2981): PHASE2 1262, PHASE1 575, PHASE1|PHASE2 473,
  PHASE3 326, *missing* 176, NA 54, EARLY_PHASE1 51, PHASE2|PHASE3 44, PHASE4 20.
- **Missing vs `NA`**: here, `phases` is missing exactly for the non-interventional studies
  (171 OBSERVATIONAL + 5 EXPANDED_ACCESS = 176). `NA` is an explicit value meaning
  "interventional, phase not applicable", e.g. device or behavioral trials. These are two different buckets.
- **A Phase filter matches any element of the list.** `AREA[Phase]PHASE2` includes PHASE1+PHASE2 trials:

  | `filter.advanced` (with `query.intr=pembrolizumab`) | totalCount |
  |---|---|
  | `AREA[Phase]PHASE2` | 1779 = 1262 + 473 + 44 |
  | `AREA[Phase]PHASE1` | 1048 = 575 + 473 |
  | `AREA[Phase]PHASE1 AND AREA[Phase]PHASE2` | 473 (only multi-phase trials) |
  | `AREA[Phase]PHASE2 AND NOT AREA[Phase]PHASE1` | 1306 |
  | `AREA[Phase](PHASE2 OR PHASE3)` | 2105 |
  | `AREA[Phase]NA` | 54 |
  | `AREA[Phase]MISSING` | 176 |

  Consequence: per-phase counts from the API overlap (1779 + 1048 > 2981 − 176). Counting
  "trials per phase" either double-counts multi-phase trials or needs an explicit combined
  bucket such as "Phase 1/2".
- `AREA[Phase]Phase 2` (human-readable value) is accepted and gives the same 1779.
  Use the enum form anyway.

## 5. Dates

- Start date precision is mixed. In the pembrolizumab set: `YYYY-MM-DD` 2753, `YYYY-MM` 223, missing 5.
  Example of a month-only date: NCT07484139 `"2026-09"` ESTIMATED. No `YYYY`-only start dates were seen.
- `startDateStruct.type`: ACTUAL 2616, ESTIMATED 305, **absent 60**. Of the 60, 55 have a date
  but no type, and all 55 were first submitted between 2013 and 2016 (legacy records).
  The other 5 have no start date at all.
- Anticipated dates: 39 studies have a start date in the future, all `ESTIMATED`.
  **266 `ESTIMATED` start dates are already in the past**: sponsors haven't updated
  the record, so `ESTIMATED` doesn't mean "future".
- Working range syntax (all values given with `query.intr=pembrolizumab`):

  | `filter.advanced` | totalCount |
  |---|---|
  | `AREA[StartDate]RANGE[2020-01-01,2020-12-31]` | 263 |
  | `AREA[StartDate]RANGE[2020-01,2020-12]` | 263 |
  | `AREA[StartDate]RANGE[2020,2020]` | 263 |
  | `AREA[StartDate]2020` | 263 |
  | `AREA[StartDate]RANGE[2020-01-01,MAX]` | 1838 |
  | `AREA[StartDate]RANGE[MIN,2019-12-31]` | 1138 |
  | `AREA[StartDate]MISSING` | 5 |
  | `AREA[StartDateType]ESTIMATED` | 305 |

  The bounds are inclusive. 1838 + 1138 + 5 = 2981 reconciles exactly.
- **A month-only date is indexed as the first day of the month.** NCT07484139 (`2026-09`) matches
  `RANGE[2026-09-01,2026-09-01]` but **not** `RANGE[2026-09-02,2026-09-28]` or
  `RANGE[2026-09-15,2026-10-15]`. Year and month buckets are therefore safe. Ranges that start or
  end mid-month mis-assign partial dates without any warning.

## 6. Pagination

- `pageSize` is capped at 1000: `pageSize=5000` returned 1000 studies, with no error or warning.
- `nextPageToken` is an opaque string (e.g. `ZVNj7o2Elu8o3lpzXcinvarumpOQJJxuZvaj2_IYkA`).
  It is absent on the last page. Per the spec, a page may also have an empty `studies` array.
- `totalCount` is returned only on the first page. `countTotal=true` is ignored on later pages.
- **The token is not bound to the query.** Reusing a pembrolizumab token with `query.intr=nivolumab`
  returned HTTP 200 with studies, giving silently wrong results. The client must resend identical
  params on every page; only `pageSize` and `pageToken` may differ.
  A malformed token gives `400 Incorrect pageToken format`.
- Full fetch (`query.intr=pembrolizumab`, pageSize 1000, projected): 3 pages, about 4.6 s,
  **2981 records = totalCount 2981, 2981 unique NCT IDs**. The count reconciles, with no duplicates across pages.
- Results are unsorted by default; `sort` accepts only date and numeric fields, plus `@relevance`.

## 7. `fields` projection

- Syntax: `fields=NCTId|BriefTitle|Phase|…` (pipe-separated). Commas also work.
  Piece names (`Phase`), dotted paths (`protocolSection.designModule.phases`) and module names
  (`IdentificationModule|DesignModule|ConditionBrowseModule`) are all accepted.
  The nested JSON structure is kept; only the requested leaves are returned.
- An unknown name gives `400 Parameter 'fields' contains invalid field name: 'NotAField'`.
- Size: 100 pembrolizumab studies take 6,027,758 bytes unprojected and 170,147 bytes with the
  20-field projection used by the script, a **97.2% reduction**. Full records are very large
  because of results sections and per-site contact lists, so always project.
- Projecting `LocationCountry` returns `locations: [{"country": …}]` per site, with no names or
  contact data. Unprojected records contain personal contact data (names, emails, phones),
  which is another reason to always project.

## 8. MeSH normalization (`derivedSection`)

- **Aliases map to the same intervention MeSH term.** Every alias-only intervention name maps to
  MeSH `pembrolizumab` (`C582435`): `Keytruda` 879/879, `MK-3475` 718/718, `KEYTRUDA®` 305/305,
  `MK3475` 87/87, `Keytruda®` 74/74, `keytruda` 13/13.
- The API's synonym expansion also merges aliases: `query.intr` = pembrolizumab, Keytruda, MK-3475,
  MK3475 or lambrolizumab all give **2981**. `AREA[InterventionMeshTerm]pembrolizumab` and
  `…Keytruda` both give 2570.
- Exact matching is available with `EXPANSION[...]`:
  `AREA[InterventionName]EXPANSION[None]pembrolizumab` → 305,
  `EXPANSION[Term]` → 2498, `EXPANSION[Concept]` (default) → 2575.
- Coverage: 2570 / 2981 pembrolizumab-query studies carry the pembrolizumab intervention MeSH term.
  **435 / 2981 have no condition MeSH term at all.**
- **Derived condition MeSH terms contain spurious terms.** 201 pembrolizumab studies carry
  `Parkinson Disease 4, Autosomal Dominant Lewy Body` even though their conditions are cancers,
  e.g. NCT03631407 (`["Colorectal Neoplasms"]`) and NCT03834519 (`["Prostatic Neoplasms"]`).
  This is probably an automated mapping triggered by PD-1/PD-L1 text (not verified).
  Use condition MeSH with care, and keep `conditions` (raw) as the cited source value.
- Intervention MeSH `ancestors` are chemical classes (e.g. "Purines", "Amino Acids"),
  not drug classes, so they are not useful for grouping.

## 9. Locations

- Countries come from a controlled vocabulary: `/stats/field/values?fields=LocationCountry` lists
  226 distinct values, all English short names. There are no `USA`/`United States` style duplicates.
  The 81 countries seen in the pembrolizumab set were all clean.
  Quirks in the global list:
  - `"Bonaire, Saint Eustatius and Saba "` has a **trailing space**. Strip values before comparing.
  - `Turkey (Türkiye)`, `Côte d’Ivoire` (curly apostrophe) and `Burma` (not Myanmar) are the
    exact strings used.
  - Historic entities still appear: `Serbia and Montenegro`, `Federal Republic of Yugoslavia`,
    `Netherlands Antilles`.
  - Territories are separate values: `Puerto Rico`, `Guam`, `Hong Kong`, `Macau`, `Virgin Islands`.
    Whether to roll them up (e.g. Puerto Rico into the United States) is a product decision.
- Missing: 213 / 2981 pembrolizumab studies have no locations (`AREA[LocationCountry]MISSING` → 213).
  No location row had an empty `country`. Globally, 60,950 studies have no LocationCountry.
- Location rows are per site, so a study repeats each country many times. The maximum seen was
  1660 rows for one study; NCT03486873 has 51 countries and 782 rows. Count *studies per country*
  by deduplicating countries within each study, and note that one study then counts toward several countries.
- **Aug 2025 change**: the official API page reportedly says that since the
  modernized data ingest (Aug 26, 2025), location and geopoint data come from a different
  database for geographic data. I could not confirm this on the primary source: the
  clinicaltrials.gov pages are rendered by JavaScript and did not render for fetching.
  Treat it as unverified. Countries look clean and consistent today. If location fields
  change shape or naming again, it should show up as new or changed values in
  `/stats/field/values?fields=LocationCountry`.

## 10. `/stats/field/values` and `/stats/field/sizes`

- `/stats/field/values?fields=Phase` returns, for each field: `type`, `piece`, `field`,
  `missingStudiesCount`, `uniqueValuesCount`, and `topValues[]` (`value`, `studiesCount`)
  computed over **the whole database**. Example for Phase: NA 239,004, PHASE2 90,598, PHASE1 66,003,
  PHASE3 50,033, PHASE4 35,959, EARLY_PHASE1 6,506, missing 144,011.
- **It does not accept query or filter params.** Both `query.intr=…` and `filter.advanced=…` give
  `400 Invalid prefix in parameter name`. It cannot count values within a filtered subset, so
  subset aggregation has to be done client-side over fetched records, or by issuing one
  `countTotal` query per bucket.
- `/stats/field/sizes?fields=Phase` gives list-size distributions: 1 phase 438,573 studies, 2 phases 24,765.
  It returns 404 for non-list leaves such as `LocationCountry`.

## 11. Rate limiting

- The spec doesn't mention a rate limit, and no rate-limit headers appear on 200 responses.
- **Observed**: about 10 back-to-back requests succeed, then the API returns `HTTP 429`
  with a small HTML body (`429 Too Many Requests`) and **no `Retry-After` header**.
  It recovered within about 0.2 s. Pacing at 1 request every 1.2 s sustained 60/60 successes.
  The script paces at 1.3 s and made 68 requests with 0 × 429.
- Retrieval must pace requests itself and retry 429 with backoff. A 429 is HTML, not JSON,
  so don't parse it as an API error.

## 12. Sizing (totalCount on 2026-10-17)

| Query | totalCount |
|---|---|
| `query.intr=pembrolizumab` | 2,981 |
| `query.cond=breast cancer` | 16,913 |
| `query.cond=non-small cell lung cancer` | 8,601 |
| `query.cond=diabetes` | 24,522 |
| all studies | 607,349 |

At 1000 per page and about 1.3 s per request, a full projected fetch takes about 3 pages for
pembrolizumab and about 25 pages (about 35 s) for diabetes. Projection is required to keep
payloads small, about 1.7 KB per study with 20 fields.

## 13. Other syntax notes

- Comma or pipe lists: `filter.overallStatus=RECRUITING|NOT_YET_RECRUITING`, `filter.ids=NCT…,NCT…`.
- If any `query.*` param contains only NCT IDs, filters are ignored (per the spec).
- `query.*` affects relevance ranking; `filter.*` and `postFilter.*` don't, and are otherwise equivalent.

## 14. Quoting values in `filter.advanced` and `query.*` (verified 2026-10-17)

| Params | totalCount |
|---|---|
| `filter.advanced=AREA[InterventionName]pembrolizumab` | 2575 |
| `filter.advanced=AREA[InterventionName]"pembrolizumab"` | 2575 |
| `filter.advanced=AREA[InterventionName]"pembrolizumab (KEYTRUDA®)"` | 43 |
| `filter.advanced=AREA[InterventionName]pembrolizumab (KEYTRUDA®)` (unquoted) | 559 |
| `filter.advanced=AREA[InterventionName]"nab-paclitaxel"` / unquoted | 1120 / 998 |
| `filter.advanced=AREA[InterventionName]"foo \"bar\""` | 0 |
| `AREA[InterventionName]"pembrolizumab" AND AREA[Phase](PHASE2 OR PHASE3) AND AREA[StartDate]RANGE[2015-01-01,MAX]` | 1887 |
| `AREA[InterventionName]"pembrolizumab" AND AREA[StartDate]RANGE[2015-01-01,2019-12-31]` | 990 |
| `query.cond=non-small cell lung cancer` | 8601 |
| `query.cond="non-small cell lung cancer"` (phrase) | 8078 |
| `query.cond=non-small cell lung cancer` + `filter.overallStatus=RECRUITING` | 1297 |

- Quoting a one-word value inside `AREA[...]` does not change the result. An unquoted
  multi-word value is not one value: parentheses are parsed as grouping. So
  intervention names are always quoted.
- A backslash-escaped quote inside a quoted value matched nothing. The escape
  semantics could not be confirmed, so values containing `"` are rejected.
- Quoting a `query.*` value turns term search (with synonym expansion) into a phrase
  match: 8601 vs 8078 for NSCLC. `query.*` values are sent raw, and values containing
  Essie syntax are rejected.
