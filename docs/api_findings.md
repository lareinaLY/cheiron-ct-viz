# ClinicalTrials.gov API v2 — verified findings

## Expectations vs. findings

| Assumption before exploring | What the data showed | Design consequence |
|---|---|---|
| `query.intr=X` returns trials that test drug X | It also matches titles, arm labels, keywords, intervention/arm descriptions and MeSH ancestors, with synonym expansion. 336 / 2981 pembrolizumab hits mention the drug only in text or comparator arms (§2) | Default match mode is the intervention name or derived intervention MeSH term (`AREA[InterventionName]` OR `AREA[InterventionMeshTerm]`, §16). `query.intr` is an opt-in `broad` mode, echoed in `meta.match_mode` |
| Derived MeSH terms are a clean normalization layer | Intervention MeSH reliably merges aliases (Keytruda, MK-3475 → pembrolizumab, C582435; ONO-4538, BMS-936558 → Nivolumab, which name matching does not expand, §16). Condition MeSH has spurious terms (201 cancer trials tagged "Parkinson Disease 4…"), and 435 / 2981 studies have none. MeSH is study-level, not tied to an intervention (§8) | Drugs are grouped by study-level intervention MeSH (id + term). Conditions use raw strings. No intervention↔MeSH linking is attempted |
| `ESTIMATED` start dates are future dates | 266 `ESTIMATED` start dates are already in the past, and 55 legacy records have a date but no type (§5) | Estimated dates are counted and flagged (`estimated_count`), not excluded. A missing type is `"unknown"` |
| A trial has one phase | `phases` is a list; 24,765 studies have two. A Phase filter matches any element, so per-phase API counts overlap (§4) | Multi-phase trials get their own mutually exclusive bucket ("Phase 1/Phase 2") |
| One location row per country | Locations are per site, up to 1660 rows for one study, with repeated countries (§9) | Countries are deduplicated within a study. Country buckets overlap across multi-country trials, and the result is flagged `buckets_overlap` |
| `pageSize` can be set as large as needed | It is silently capped at 1000, with no error (§6) | Retrieval pages at 1000 and reconciles the retrieved count against `totalCount` |
| A `pageToken` is tied to its query | The token is not bound to the query; reusing it with other params returns wrong results with HTTP 200 (§6) | Page requests resend frozen params; only `pageToken` changes |
| The API publishes a rate limit or `Retry-After` | It publishes neither. After about 10 rapid requests it returns HTML 429 with no `Retry-After`; 1 request / 1.2 s is sustainable (§11) | Retrieval paces requests itself and retries 429 with exponential backoff |
| `/stats/field/values` can count values within a query | It rejects `query.*` and `filter.*` params with 400 and only covers the whole database (§10) | Subset aggregation is done client-side over fetched records, or, past the record cap, with one `countTotal` request per bucket filter (§17) |
| Past the record cap, a distribution can only be estimated from the records fetched | Bucket filters that partition the cohort give exact per-bucket counts; on pembrolizumab they equal the full fetch in every bucket of four dimensions (§17). The first 10,000 breast cancer trials by registration date understate Phase 2 by 4.1 points and overstate Not Applicable by 6.2 | API-countable dimensions use the count_per_bucket route instead of truncating; meta states the route |

## Open ambiguities

These are choices a reasonable person could make differently. Each states our choice and the alternative.

| Question | Our choice | Alternative |
|---|---|---|
| Count unit | Trials (studies), stated in `meta.count_unit` | Sites, arms or enrolled participants |
| Multi-phase bucketing | Own combined bucket ("Phase 1/Phase 2"); buckets are mutually exclusive | Count the trial in every phase it lists (totals exceed the number of trials), or assign it to its highest phase |
| Territories | Reported as the API gives them (Puerto Rico, Guam, Hong Kong are separate) | Roll territories up into a sovereign country |
| Drug matching | `AREA[InterventionName]` OR `AREA[InterventionMeshTerm]` by default (§16); `query.intr` as opt-in `broad` | Broad by default, name only (misses code-name-only trials), or MeSH only (misses names without a MeSH term) |
| Non-drug nodes in networks | Drug nodes are intervention MeSH terms filed under Chemicals and Drugs: supplementary concepts (C ids) and descriptors with a MeSH tree number in branch D (§18), minus two branch-D comparators (Saline Solution, Placebos). Dropped terms are listed in `meta.network.excluded_terms`, descriptors missing from the trees are kept and listed in `meta.network.unmatched_terms` | Filter nodes by intervention type (DRUG/BIOLOGICAL), which needs an intervention↔MeSH link the API doesn't provide; by `browseLeaves[].relevance`, which the API does not serve (§8); or by a deny-list of observed terms, which does not generalize to other drugs |
| Trials without intervention MeSH | `Unknown` bucket, with a meta warning giving the count | Fall back to the raw intervention name as the bucket key, which splits aliases into separate buckets |
| "Fewest" questions ("which countries have the fewest trials") | Not a ranking: `ranking_intent` covers most/top/leading only, so the chart is untrimmed and sorted by count, with the smallest values last | Trim to the N smallest buckets, which needs a sort direction in the plan |
| Year-only start date in monthly views | `Unknown` bucket for `start_month` (citing the raw year); counted normally for `start_year` | Place it in January (the API's indexing convention) |
| Enum value we have never seen (e.g. a new phase) | Record kept; the field is `Unrecognized` (raw value cited) and the trial goes to an `Unrecognized` bucket, distinct from `Unknown`; one meta warning per distinct value | Skip the record (one new value would drop trials that are fine on every other dimension), or map it to the nearest known value |
| Start date in an unrecognized format (e.g. `"soon"`) | Record kept; `Unknown` on start year and start month (citing the raw string), counted everywhere else, with a warning naming the value | A separate `Unrecognized` bucket on the time axis, which has no position for it either |

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
- **Which phase combinations exist** (whole registry, 2026-10-18, `AREA[Phase]A AND
  AREA[Phase]B` for all 15 pairs): only PHASE1+PHASE2 (17,142) and PHASE2+PHASE3 (7,623);
  the other 13 pairs match 0. 17,142 + 7,623 = 24,765, the two-phase count above. So the
  mutually exclusive phase buckets are the 6 single phases, these 2 pairs, and missing.
- **An exact phase set is "listed phases present AND NOT any other phase":**
  `AREA[Phase]PHASE2 AND NOT AREA[Phase](EARLY_PHASE1 OR PHASE1 OR PHASE3 OR PHASE4 OR NA)`
  with `query.intr=pembrolizumab` gives 1262, the PHASE2-only count above. Wrapped in
  parentheses it AND-joins with other clauses in either order (1250 with
  `AREA[StartDate]RANGE[2015-01-01,MAX]` before or after). Used by the count_per_bucket
  route (§17).

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
- Results are unsorted by default; the spec says this is "for a performance reason". `sort`
  accepts only date and numeric fields, plus `@relevance`, with at most 2 keys
  (`field:asc|desc`; studies missing the field come last). `NCTId` is a string, so it cannot
  be a tie-breaker, and no unique numeric field exists.
- Verified 2026-10-18 on `query.cond=breast cancer` (16,913 matches): the default order is
  repeatable between calls but follows no date (its first page shares 62 of 1,000 studies
  with the `LastUpdatePostDate:desc` first page). Within ties, sorted order was repeatable but
  not by NCT ID. At the 10,000 cap, `LastUpdatePostDate:desc` cut through a date shared by 6
  matches (2 included); `StudyFirstPostDate:desc` ended on 2016-07-15 with all 4 of its
  matches included.
- Every `/studies` query is sent with `sort=StudyFirstPostDate:desc`, so a capped fetch is
  "the most recently registered trials" (decisions.md). This only matters for dimensions
  that cannot use the count_per_bucket route (§17); API-countable dimensions are no longer
  truncated.
- `pageSize=0` is not a count-only request: it is ignored and returns the default 10
  studies with totalCount.

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
- **Derived intervention MeSH terms include mis-mappings and non-treatments** (2575
  pembrolizumab trials, 2026-10-18): `Taxes` (D013660, 48 trials, from "taxane"),
  `Automobiles` (D001336, from "CAR" T-cell therapy), `Single Person` (D012847, 4), `Congresses
  as Topic` (D003226, 3), `Solutions` (D012996, 4, from "solution for injection"). Comparators
  appear too: `Standard of Care` (8), `Observation` (10), `Watchful Waiting` (7). No trial
  carries `Placebos` (D010919); placebo arms map to `Saline Solution` (D000077330; 8 of its 18
  trials list an intervention named "Placebo"). Diagnostics are frequent and large:
  `Specimen Handling` (81), `Magnetic Resonance Spectroscopy` (84, mapped from "Magnetic
  Resonance Imaging"), `Biopsy` (49). All of these are outside MeSH tree branch D and are
  now dropped from drug networks by the tree rule (§18).
- **`browseLeaves` and `browseBranches` are in the OpenAPI schema but not served.** On
  2026-10-18 full records (`/studies/NCT07198074`, `/studies/NCT02142738`) carry only
  `meshes` and `ancestors` in both browse modules; projecting
  `InterventionBrowseLeafRelevance` returns an empty module, and
  `AREA[InterventionBrowseLeafRelevance]LOW` matches 0 studies. The per-term `relevance`
  (HIGH/LOW) is therefore not available as a node filter.

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
  `countTotal` query per bucket (the count_per_bucket route, §17).
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

Measured at the record cap (2026-10-18, `examples/i_breast_cancer_by_phase.*`, no
cache): `query.cond=breast cancer` by phase gave totalCount 16,913, 10,000 retrieved
(10 pages), `truncated=true`, coverage 59.1%, 0 skipped. With the default order: 13.8-14.0 s
wall time over two runs (about 12.2 s retrieval). With `sort=StudyFirstPostDate:desc`
(the current example): 13.9 s (12.1 s retrieval, 1.3 s normalize, 0.4 s aggregate), a
158.6 MB tracemalloc peak in the process, and a 35.2 KB response (citations capped at 20
per bucket); sorting added no measurable time. The two orders select different subsets
(e.g. Phase 2: 2,094 vs 1,699; Not Applicable: 2,952 vs 3,557), which is why the order
is named in `meta.assumptions` with the title suffix and `data_quality="partial"`.

Since the count_per_bucket route (§17), breast cancer by phase is no longer truncated:
10 `/studies` requests, 12.0 s wall time, exact counts for all 16,913 matches, a 20.4 KB
response (`examples/i_breast_cancer_by_phase.*`).

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

## 15. Record-level data quality baseline (verified 2026-10-18)

Every full fetch so far normalized without a single unreadable record. This is the
baseline the degradation thresholds (`DEGRADED_SKIP_RATIO`, `REFUSE_SKIP_RATIO`,
`MIN_SKIPPED_FOR_DEGRADED`) are anchored on.

| Query | Retrieved | Skipped (structural) | Unrecognized values |
|---|---|---|---|
| `AREA[InterventionName]"pembrolizumab"` | 2575 | 0 | 0 |
| `query.cond=non-small cell lung cancer` + `filter.overallStatus=RECRUITING` | 1297 | 0 | 0 |

- The same 0 skipped was seen in every example run on 2026-10-17 and 2026-10-18
  (`examples/`, dataTimestamp `2026-10-16T09:00:05`), and again with the stricter
  type-checking normalizer on 2026-10-18 (same dataTimestamp).
- `GET /studies/enums` on 2026-10-18 matched our `Phase`, `Status`, `StudyType`,
  `AgencyClass` and `InterventionType` enums exactly (startup drift check: no new values).
- Each request logs this rate per cohort (`normalize cohort= retrieved= skipped=
  skip_ratio= unrecognized= band=`), so a baseline-relative alert can be built on it.

## 16. Does exact `AREA[InterventionName]` miss brand and code names? (verified 2026-10-18)

Area name check: `/studies/search-areas` lists `InterventionMeshTerm` as a synonym-enabled
piece (`isSynonyms: true`). It indexes `derivedSection.interventionBrowseModule.meshes[].term`.
Counts use `countTotal=true&pageSize=1`. Set comparisons use full NCT ID fetches.

| Query | pembrolizumab | nivolumab |
|---|---|---|
| A `AREA[InterventionName]"<generic>"` | 2575 | 1675 |
| B `AREA[InterventionName]"<brand>"` (Keytruda / Opdivo) | 2575, B = A | 1675, B = A |
| B, `EXPANSION[None]` (literal brand only) | 67, all in A | 36, all in A |
| C `AREA[InterventionName]"<code>"` (MK-3475 / BMS-936558) | 2575, C = A | 11, **7 not in A** |
| C2 `AREA[InterventionName]"ONO-4538"` | n/a | 22, **21 not in A** |
| `AREA[InterventionName]"MDX-1106"` / `"MK3475"`, `"lambrolizumab"` | 2575, = A | 1675, = A |
| D `AREA[InterventionName](pembrolizumab OR Keytruda OR MK-3475)` (unquoted) | 2576 | — |
| E `AREA[InterventionMeshTerm]"<generic>"` | 2570 | 1712 |
| E − A / A − E | 67 / 72 | 85 / 48 |
| A OR E | 2642 | 1760 |
| A OR `AREA[InterventionOtherName]"<generic>"` | 2644 | 1748 |
| F `query.intr=<generic>` | 2981 | 2033 |

- `OR` works inside `AREA[...]`: D = |A ∪ unquoted C|. Unquoted `MK-3475` matches one extra
  trial, NCT02430363 (intervention named `"MK - 3475"`). The quoted form the query builder
  sends matches exactly A.
- **The API's concept expansion is per drug, not general.** It merges Keytruda, MK-3475,
  MK3475 and lambrolizumab into pembrolizumab, and Opdivo and MDX-1106 into nivolumab, but
  **not BMS-936558 or ONO-4538**. 28 nivolumab trials are registered only under those code
  names and missed by exact matching, e.g. NCT02261285 (sole intervention `"ONO-4538"`, no
  otherNames, MeSH `Nivolumab`). All 28 carry the nivolumab MeSH term. Pembrolizumab has no
  such misses.
- **A second gap, for both drugs: the drug only in `InterventionOtherName`.** Pembrolizumab
  E − A is 67 trials, nivolumab 57 (plus the 28 code-name trials). Some are real trials of the
  drug under another name, e.g. NCT05127590 (`"RBN-2397"` + otherNames Pembrolizumab,
  Keytruda) or NCT07662720 (misspelled `"Pembolizumab"`). Others are class-level placeholders
  that list the drug as one allowed option, e.g. NCT07419932 (`"PD(L)-1 inhibitor"`, otherName
  Pembrolizumab) or NCT04115410 (`"PD-1 inhibitor"`; MeSH lists nivolumab, pembrolizumab and
  atezolizumab).
- **MeSH alone is not a superset of exact name.** A − E is 72 (pembrolizumab) and 48
  (nivolumab). These are trials whose names mention the drug but whose derived MeSH lacks
  it, mostly combination strings, e.g. NCT05389527 (`"Pembrolizumab+Lenvatinib"`, no MeSH) or
  NCT07581509 (`"US-Licensed Keytruda"`, no MeSH). Replacing A by E would lose these.

**Conclusion.** Exact name matching undercounts, and how much depends on the drug. So
`intervention_name` mode now sends
`(AREA[InterventionName]"X" OR AREA[InterventionMeshTerm]"X")`. The two signals complement
each other: code-name-only trials are reachable only via MeSH, and combination-string names
only via the name. The match stays at the intervention level and still excludes the
text-only matches of `broad` mode (336 for pembrolizumab, §2).

| Drug | Name only (before) | Name OR MeSH (after) | Recovered | `broad` |
|---|---|---|---|---|
| pembrolizumab | 2575 | 2642 | +67 | 2981 |
| nivolumab | 1675 | 1760 | +85 (28 code-name-only) | 2033 |

- The parentheses are required. AND binds tighter than OR: without them, pembrolizumab with
  `AREA[Phase](PHASE2 OR PHASE3)` gives 2611 instead of 1951 (name only: 1915).
- A quoted name with parentheses still works (`"pembrolizumab (KEYTRUDA®)"`: 43, the MeSH
  branch adds 0). An unknown drug matches 0 in both branches, so no MeSH lookup or fallback
  is needed.
- **Known cost:** the added trials include class-level placeholders that list the drug as one
  allowed option in otherNames ("PD(L)-1 inhibitor" → Pembrolizumab), and any MeSH
  mis-mapping onto the drug's term (§8). Both are stated in `meta.search_semantics`.

## 17. Exact counts past the record cap: one countTotal request per bucket (verified 2026-10-18)

For a dimension whose buckets can be written as `filter.advanced` clauses that partition
the cohort, `countTotal=true` on each clause (AND-joined with the cohort's filters) gives
each bucket's exact count, however many trials match. `pageSize` is set to the citation
sample size (10), so each request also returns that many studies to cite.

Clause syntax checked on `query.cond=breast cancer` (16,913):

| Clause | totalCount | Note |
|---|---|---|
| `filter.overallStatus=RECRUITING` / `AREA[OverallStatus]RECRUITING` | 2451 / 2451 | Same for COMPLETED (7752) and UNKNOWN (2511); the AREA form AND-joins with a cohort's own status filter |
| `AREA[OverallStatus]MISSING` | 0 | |
| `AREA[LeadSponsorClass]INDUSTRY` / `OTHER` / `UNKNOWN` / `MISSING` | 3445 / 12370 / 5 / 0 | The field named in the OpenAPI `LeadSponsorClass` piece |
| `sort=StartDate:asc` / `:desc`, `pageSize=1` | 1971-04 / 2028-01-01 | Earliest and latest start dates bound the year buckets; missing dates sort last both ways |

Phase uses the exact-set clauses of §4, start year the inclusive `RANGE` of §5, and each
dimension's Unknown bucket its `MISSING` clause, so the remainder (totalCount minus the
summed buckets) must be 0. A nonzero remainder would mean trials in no bucket (e.g. a new
phase pair) or in several, and the cohort is fetched instead.

**Reconciliation against the fetch route** (`tests/test_live_count_route.py`, pembrolizumab
in `intervention_name` mode, 2,642 trials, below the cap so the fetch is complete;
dataTimestamp 2026-10-16T09:00:05): the forced count route equals the fetch route **in
every bucket** for phase, start year (20 buckets, 2008-2027 plus Unknown), overall status
and lead sponsor class, with remainder 0. Phase, both routes:

| Early Phase 1 | Phase 1 | Phase 1/Phase 2 | Phase 2 | Phase 2/Phase 3 | Phase 3 | Phase 4 | Not Applicable | Unknown |
|---|---|---|---|---|---|---|---|---|
| 40 | 541 | 433 | 1163 | 41 | 314 | 16 | 27 | 67 |

**Truncation bias** (`query.cond=breast cancer` by phase, 16,913 matches, same
dataTimestamp 2026-10-16T09:00:05 for both runs). The fetch route keeps the 10,000 most
recently registered trials (`StudyFirstPostDate:desc`; rerun today it reproduces the
previous run's numbers exactly). The count route is exact: 10 `/studies` requests (page 1
of the fetch, which gives totalCount, then 9 buckets), 12.0 s wall time vs 13.1 s for the
truncated fetch, remainder 0.

| Bucket | Fetch route (first 10,000) | Share | Count route (all 16,913) | Share | Bias | Missed by truncation (share of the 6,913) |
|---|---|---|---|---|---|---|
| Early Phase 1 | 144 | 1.4% | 232 | 1.4% | +0.1 pp | 88 (1.3%) |
| Phase 1 | 846 | 8.5% | 1,639 | 9.7% | -1.2 pp | 793 (11.5%) |
| Phase 1/Phase 2 | 503 | 5.0% | 876 | 5.2% | -0.1 pp | 373 (5.4%) |
| Phase 2 | 1,699 | 17.0% | 3,567 | 21.1% | -4.1 pp | 1,868 (27.0%) |
| Phase 2/Phase 3 | 77 | 0.8% | 148 | 0.9% | -0.1 pp | 71 (1.0%) |
| Phase 3 | 604 | 6.0% | 1,411 | 8.3% | -2.3 pp | 807 (11.7%) |
| Phase 4 | 180 | 1.8% | 345 | 2.0% | -0.2 pp | 165 (2.4%) |
| Not Applicable | 3,557 | 35.6% | 4,967 | 29.4% | +6.2 pp | 1,410 (20.4%) |
| Unknown | 2,390 | 23.9% | 3,728 | 22.0% | +1.9 pp | 1,338 (19.4%) |

The 6,913 older trials the cap dropped are much more often drug trials in Phase 2 and 3
(38.7% vs 23.0% among the retrieved), and less often Not Applicable or without a phase.
A truncated chart shows the mix of recent registrations, not the cohort. This is the bias the title suffix and `data_quality="partial"`
warn about on dimensions that still have to be fetched.

## 18. MeSH tree numbers as a drug node filter (verified 2026-10-18)

**Source.** NLM's MeSH tree file `mtrees2025.bin`,
`https://nlmpubs.nlm.nih.gov/projects/mesh/2025/meshtrees/mtrees2025.bin` (2,722,561 bytes,
Last-Modified 2026-01-08; one ` Preferred Name;TreeNumber` line per tree position, each with
a leading space). It is linked from the year directories under
`https://nlmpubs.nlm.nih.gov/projects/mesh/`, not from the download page
(`https://www.nlm.nih.gov/databases/download/mesh.html`). On 2026-10-18 there is **no 2026
file**: `/2026/meshtrees/mtrees2026.bin` and `/MESH_FILES/meshtrees/mtrees2026.bin` redirect to
an error page, and the `MESH_FILES/meshtrees/` listing is empty, so 2025 is the newest
published release.

**License.** Free to use under NLM's Terms and Conditions for MeSH
(`https://www.nlm.nih.gov/databases/download/terms_and_conditions_mesh.html`), which is
not a public-domain dedication: redistributors must acknowledge NLM as the source, must
not imply NLM endorsement, and must either keep the data current or identify the MeSH
version used. The derived file and every network response name the version ("NLM MeSH
2025" in the drug-node assumption).

**Derived file.** `scripts/fetch_mesh_trees.py` writes
`src/ctviz/data/mesh_trees.json`, `{preferred_name: [tree_numbers]}`, key-sorted: 30,954
descriptors, 2,187,017 bytes. That is under the 3 MB budget, so full tree numbers are kept
(the script falls back to first letters only above it). The file has descriptors (D ids)
only. Supplementary concept records (C ids, e.g. pembrolizumab C582435) have no tree
numbers.

**Rule.** A drug node is kept when its id is a C id, or when its preferred name has any
tree number in branch D (Chemicals and Drugs). Branch-D comparators matched by id (Saline
Solution D000077330, Placebos D010919) are dropped. A descriptor whose name is not in the
file is kept and listed as `unmatched_in_tree`.

**Live check** (pembrolizumab, `intervention_name` match mode, 2,642 trials, dataTimestamp
2026-10-16T09:00:05):
- 712 distinct intervention MeSH keys: 388 C ids and 324 D ids. **All 324 D ids match the
  tree file by exact preferred name** (match rate 100%; no unmatched terms).
- **117 terms are excluded, in 456 trials**: 116 by branch (E01 diagnostics, E02 therapies
  incl. radiotherapy, E04 surgery, E05 investigative techniques, G/N/F/I/J
  mis-mappings) and Saline Solution as a comparator. Top: Magnetic Resonance Spectroscopy
  (84 trials, E05), Specimen Handling (81, E01/E05), Radiotherapy (79, E02), Biopsy (50, E01), Taxes
  (49, N03), Radiation (47, G01), Drug Therapy (43, E02), Radiosurgery (42, E02). None of the 117
  is a drug.
- Kept: Platinum (D01), Carboplatin (D02). Excluded: Radiotherapy (E02).
- The former deny-list under the rule: Standard of Care (N04/N05), Observation (E05),
  Watchful Waiting (N04), Taxes (N03), Single Person (F01/I01/M01/N01), Automobiles (J01)
  and Congresses as Topic (N03) are excluded by branch; Saline Solution by the comparator
  list. **Solutions (D012996, 4 trials) is now kept**: it is D26.776 (pharmaceutical
  solutions), so the branch rule cannot tell it from a drug.
- **Class-level descriptors pass the rule.** 16 kept D ids sit only in D26 (pharmaceutic
  preparations) or D27 (chemical actions and uses), e.g. Immune Checkpoint Inhibitors (24
  trials), Adjuvants, Pharmaceutic (5), Solutions (4), Antineoplastic Agents (1). They are
  drug classes or dosage forms rather than drugs. They are kept as nodes of type
  `drug_class` and counted in `meta.network.drug_class_nodes_total`.
- **Known gap: C ids are kept without inspection.** Supplementary concept records have a
  class (1 chemical, 2 protocol, 3 disease, 4 organism) and a mapped-to descriptor, but
  neither is in the tree file. Classifying them would need NLM's supplementary concept file
  (`https://nlmpubs.nlm.nih.gov/projects/mesh/2025/asciimesh/c2025.bin`, not downloaded or
  measured). Until then, a disease or organism SCR mapped from an intervention would be
  drawn as a `drug` node. None was noticed among the top pembrolizumab hubs, but the 388 C ids
  were not checked one by one.
- Top-10 drug co-occurrence hubs (weighted degree) are the same ten drugs before and after;
  the change is lower down. Before, Specimen Handling ranked 17, Magnetic Resonance
  Spectroscopy 18, Radiotherapy 23, Radiation 28, Biopsy 30 and Drug Therapy 34 among the
  50 shown nodes. In the sponsor-drug network, Specimen Handling and Magnetic Resonance
  Spectroscopy were drug hubs 6 and 7 (degree 67 and 64) and are now gone.

## 19. Network node ranking: plain vs weighted degree (measured 2026-10-18 10:21 PT)

Drug co-occurrence network for pembrolizumab (example h, `intervention_name` match mode,
2,642 trials), top 10 of the 50 shown nodes before and after the ranking changed from plain
degree (number of distinct neighbours) to weighted degree (sum of incident edge weights after
the `MIN_EDGE_WEIGHT` cut; decisions.md, weighted degree entry):

| Rank | Plain degree (before) | Weighted degree (after) |
|---|---|---|
| 1 | pembrolizumab | pembrolizumab (3143) |
| 2 | Nivolumab | Carboplatin (1359) |
| 3 | Carboplatin | Cisplatin (997) |
| 4 | Bevacizumab | Paclitaxel (973) |
| 5 | olaparib | Pemetrexed (795) |
| 6 | atezolizumab | Gemcitabine (518) |
| 7 | durvalumab | Fluorouracil (502) |
| 8 | lenvatinib | Nivolumab (490) |
| 9 | Ipilimumab | Oxaliplatin (388) |
| 10 | Paclitaxel | nab-paclitaxel (380) |

Plain degree ranks drugs that appear next to many different drugs, which favours other
checkpoint inhibitors and drugs listed in umbrella and platform protocols. Weighted degree
ranks drugs that repeatedly appear with the same partners, and the top 10 becomes the
platinum-doublet chemotherapy backbone pembrolizumab is combined with in practice
(carboplatin or cisplatin with paclitaxel, pemetrexed or gemcitabine). Edges are still
co-listing, not co-administration (decisions.md): Nivolumab stays in the top 10 as an
alternative or comparator, not a partner.
