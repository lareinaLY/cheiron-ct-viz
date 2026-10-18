# Pre-submission review findings

Reviewed on 2026-10-18 at `42c2f66` (main, clean tree). Read in full: CLAUDE.md, docs/architecture.md,
docs/decisions.md, docs/api_findings.md, docs/planner_eval.md, all of `src/ctviz/`. Every test file
was opened, and every example response was checked against the current `VisualizationResponse` schema
(a, e, h and i were traced field by field). The offline suite passes (651 passed, 7 live tests
deselected, 4.9 s), and `ruff check` and `ruff format --check` are clean. No live API or LLM calls
were made for this review.

Severity scale: **critical** = a wrong number or a broken invariant on a main path;
**high** = something a grader will hit or that breaks a stated guarantee; **medium** = a real
defect or gap with a limited blast radius; **low** = polish or drift.

There are **no critical findings**. The counting logic holds up under tracing. Phase exclusivity,
the count-route partition and remainder check, NCT-ID dedup, top_n trimming with hidden-trial
counts, cohort overlap, and network weights as distinct-trial sets are all correct. The most
damaging problems are presentational (stale examples, no README) and robustness gaps outside the
happy path.

---

## High

### H1. Example responses are stale and contradict the current code and docs
- **Where:** `examples/a_pembrolizumab_by_phase.response.json`, `b_…`, `d_…` (counts and semantics);
  every example except `e_…` (schema).
- **What:** a, b and d were produced before commit `0b50401` (drug = name OR MeSH). Their
  `meta.search_semantics.drug` starts "Exact intervention name: filter.advanced AREA…", a text
  that no longer exists in `retrieval/query_builder.py:40-75`. Example a reports 2575 trials, but
  the current code gives 2642 for the same request. 8 of 9 responses also lack
  `meta.cohorts_overlap`, which was added in `f718b02`; only e was refreshed.
- **Why it matters:** architecture.md:284 calls these "real responses". A grader tracing a
  sees 2575 next to api_findings §16/§17 (2642, Unknown 67 vs 51), and a semantics sentence
  the code cannot emit. This breaks the CLAUDE.md rule "Schema changes update docs and examples
  in the same change". Within the set, e's pembrolizumab series (2642) contradicts a's (2575).
- **Fix:** clear `.cache/` and regenerate all nine examples against the live API, keeping the
  same dataTimestamp if possible. Add an offline test that loads every `examples/*.response.json`
  into `VisualizationResponse`, requires every current `Meta` key to be present, and checks that
  each `search_semantics` value is in `query_builder.SEMANTICS`. That catches the next drift.
- **Effort:** 15 min to regenerate (rate-limited, about 2 min of API time) plus 10 min for the test.

### H2. No README
- **Where:** repo root. `pyproject.toml` has no `readme`.
- **What:** nothing tells a grader how to install, configure (`.env.example`), run
  (`uv run uvicorn ctviz.api.app:app`), test (`uv run pytest`, `-m live`), or where the design
  docs and examples are. Today that information is spread across architecture.md:284 and
  docstrings.
- **Why it matters:** this is the first file a grader opens. It feeds the input/output (10%)
  and system design (35%) scores.
- **Fix:** a one-page README covering what it does, a quickstart, one curl request and an
  abridged response, the env vars, the doc map, and known limits (see H4).
- **Effort:** 15 min.

### H3. Unexpected exceptions escape as unstructured 500s; one is reachable from dirty upstream data
- **Where:** `api/app.py:188-250` has no catch-all handler, yet `api/app.py:4` says "Every
  failure is returned as an ErrorResponse".
- **Concrete crash:** `aggregate/registry.py:454` `enrollment_bin` calls `max()` on an empty
  sequence when the count is negative (verified: `ValueError max() iterable argument is empty`).
  normalize accepts any integer (`normalize/study.py:388`), so one study with
  `enrollmentInfo.count: -1` turns an `enrollment_bucket` request into a plain-text 500. This is
  exactly the over-strict single-record failure that decision 68 forbids.
- **Other paths that bypass the handlers:**
  - `sqlite3.OperationalError` (database locked, disk full or corrupt) from `retrieval/cache.py:78,93`.
  - `httpx.DecodingError` and other `httpx.RequestError`s that are not `TransportError`
    (`retrieval/client.py:312` catches only `TransportError`).
  - `openai.APIResponseValidationError` (`planner/llm.py:169-180`).
  - The internal `ValueError` guards in `aggregate/group.py:91`, `spec/builder.py:339` and
    `pipeline.py:483`.
- **Fix:**
  - (a) Register `@app.exception_handler(Exception)` returning
    `ErrorResponse(code="internal_error", …)` with status 500, and log the traceback.
  - (b) In normalize, treat a negative enrollment as a value-domain failure
    (`UnrecognizedValue` → Unrecognized bucket), or have `enrollment_bin` raise
    `SourceRecordError`, and add a poison-record test.
  - (c) Widen the client's catch to `httpx.RequestError`.
- **Effort:** 15 min.

### H4. Single-process assumptions are undocumented, and the event loop blocks on CPU work
- **Where:** `retrieval/client.py:94` (`TokenBucket` per client instance), `retrieval/cache.py:68`
  (one `sqlite3` connection, no WAL, no busy timeout, synchronous calls inside async code),
  `pipeline.py:194-198` and `214` (synchronous normalize and aggregate inside `async def execute`),
  and `spec/builder.py:177,186` (`fit_network_budget` serializes the whole response twice).
- **What:**
  - With `uvicorn --workers N`, pacing becomes N requests per 1.2 s, which brings back the
    429s that api_findings §11 measured, and the workers contend for one SQLite file. No doc
    mentions workers or multiple processes (a grep for "worker" over src/docs/tests finds nothing).
  - Inside one worker, normalizing 10,000 records takes about 1.3 s of blocking CPU (api_findings
    §12). During that time `/health` and every other request stall.
  - One large request peaked at 158.6 MB (api_findings §12), and nothing limits how many run
    concurrently.
- **Why it matters:** "how does this scale?" is the obvious system-design question. The answer
  exists ("one process; pacing is per process; Postgres/AACT is the production path",
  decision 8), but it is not written down.
- **Fix:**
  - Now: add a "Deployment limits" section to architecture.md and one line to decisions.md:
    single worker by design, why, and what changes for N workers (a shared limiter in
    Redis/SQLite, a cache with WAL or another backend).
  - Optional: run normalize, aggregate and spec through `await asyncio.to_thread(...)`, and add
    an `asyncio.Semaphore` capping concurrent `/query` executions.
- **Effort:** 10 min for the docs; 20 min for `to_thread` plus a semaphore.

---

## Medium

### M1. Drug co-occurrence: the queried drug's hub undercounts the cohort, and nothing says so
- **Where:** cohort matching in `retrieval/query_builder.py:206` (name OR MeSH) vs drug nodes in
  `aggregate/registry.py:429-448` (MeSH only).
- **What:** in example h the pembrolizumab node has size 2570 while the cohort is 2642. The 72
  trials matched by name only (e.g. `"Pembrolizumab+Lenvatinib"` without the MeSH term,
  api_findings §16) contribute no edge to the hub, even when their other drugs are MeSH-mapped.
  So every hub edge weight ("co-occurs with pembrolizumab in N trials") is a lower bound by up to
  72 trials, which is 2.7% here. The 914-trial "fewer than two values" warning does not cover
  this case.
- **Why it matters:** invariant 3. The hub's edge weights are the numbers the question asks for
  (decision 66).
- **Fix:** in `build_network`, or in the pipeline when the plan has a `drug` filter, count the
  cohort trials whose MeSH list lacks the queried drug's node, and state it in meta.network and in
  an assumption ("72 of 2642 trials match pembrolizumab by name only and have no pembrolizumab
  MeSH term; they add no edges to the hub"). Do not invent edges.
- **Effort:** 20 min.

### M2. `citation_count_total` means different things on different elements, and node citation caps fill up with supporting citations
- **Where:** `aggregate/group.py:47,174` (counts every citation), `aggregate/registry.py:441-448`
  (intervention MeSH adds every raw intervention name as a supporting citation),
  `aggregate/network.py:138,275,315,326`.
- **What:**
  - On a phase Datum, `citation_count_total` equals the trial count.
  - On an `intervention_mesh` Datum it is larger (fixture: count 5, total 25).
  - On a drug node it is far larger: pembrolizumab has size 2570 and a total of 11448.
  - On an edge it is 2 × weight.
  - The 20-citation cap is applied to primary and supporting citations together, so the
    pembrolizumab node carries only 4 primary citations, i.e. 4 trials.
- **Why it matters:** a renderer that shows "20 of N source trials" will be wrong. The schema
  description ("True number of citations before the cap") is literally true but not useful.
- **Fix:** add `cited_trials_total` (distinct NCT IDs) next to `citation_count_total`, or define
  the total as distinct trials. Apply the cap to primary citations and keep supporting citations
  per cited trial.
- **Effort:** 30 min including golden updates.

### M3. No size limits on request input
- **Where:** `schemas/request.py:45-62`, `schemas/plan.py:68-75`.
- **What:** `query`, `drug`, `condition`, `sponsor` and `country` have no `max_length`, and
  `phases`/`statuses` have no `max_length` either (duplicates are allowed).
  `test_adversarial.py:136` shows that a 5,000-character question goes to the LLM unchanged.
  A 1 MB question is billed tokens. A 100,000-entry `phases` list builds a huge
  `AREA[Phase](… OR …)` URL, which upstream rejects as a 502 rather than a 422.
- **Fix:** set `max_length` on `query` (e.g. 2,000) and on free-text filters (e.g. 200), and use
  `Field(max_length=len(Phase))` plus dedup for the lists. Mirror these limits in `Filters` so
  that LLM drafts are bounded too.
- **Effort:** 10 min plus 3 adversarial test cases.

### M4. A backslash or control character can reach the Essie expression
- **Where:** `retrieval/query_builder.py:172-185` (`quotable_value`) and `157-169`
  (`free_text_value`).
- **What:** `drug='x\'` produces `(AREA[InterventionName]"x\" OR AREA[InterventionMeshTerm]"x\")`
  (verified). If Essie treats `\"` as an escape, the quoting breaks and the clause changes meaning.
  api_findings §14 says the escape semantics "could not be confirmed". That uncertainty is the same
  reason `"` is rejected, so `\` should be rejected too. Newlines and control characters pass both
  checks (verified: `'lung\ncancer*'` passes; `*` is untested Essie syntax).
- **Why it matters:** decision 77 promises that no value can change search semantics.
- **Fix:** reject `\` and the C0 control characters in both functions. Either probe `*`/`?`
  live once and reject them, or note them as untested.
- **Effort:** 10 min.

### M5. The meta warnings list is unbounded
- **Where:** `pipeline.py:569` (`Skipped {error}` per skipped record), `pipeline.py:581`, and
  `errors.py:83` (`{value!r}` of an arbitrary JSON value).
- **What:** below the refuse ratio, a 10,000-record cohort can have up to 4,999 skipped records,
  which produces 4,999 warning lines, each with an unbounded repr (e.g. a list-valued
  `briefTitle`). `normalize/unrecognized.py:52` emits one warning per distinct unrecognized raw
  date string, which is also unbounded.
- **Fix:** list the first 10 skipped NCT IDs and reasons, then add one "and N more" line. The
  full list belongs in the log. Truncate the repr to about 120 characters in `SourceRecordError`.
- **Effort:** 10 min.

### M6. Grouped charts have no size budget and no default cap on open-vocabulary dimensions
- **Where:** `spec/builder.py:45-136`. Only networks get `fit_network_budget`.
- **What:** `condition`, `lead_sponsor`, `intervention_mesh` and `country` produce one datum per
  distinct value, with up to 20 citations each. A 10,000-trial "breast cancer by condition" or
  "by lead sponsor" request returns thousands of bars and a response of several MB (not measured).
  top_n is applied only when the question asks for a ranking.
- **Fix:** either apply `DEFAULT_TOP_N_FOR_RANKING` whenever the bucket count exceeds a
  readability threshold for an open-vocabulary dimension (reported like the ranking default), or
  add a byte budget that cuts grouped citations the same way networks do.
- **Effort:** 20 min.

### M7. No end-to-end deadline, and one global queue for all users
- **Where:** `config.py:100-102,130` and `retrieval/client.py:308-337`.
- **What:**
  - One GET can take 6 attempts × 60 s timeout plus about 31 s of backoff, roughly 6.5 min.
  - Each LLM attempt can take providers × 2 tries × 60 s, and there are 2 attempts.
  - A 60-bucket count route occupies the shared limiter for at least 72 s, and every other
    user's requests queue behind it (`TokenBucket` holds the lock while sleeping, `client.py:53-58`).
  - Nothing caps the total time of a request.
- **Fix:** wrap `execute`/`plan` in `asyncio.timeout(settings.request_deadline_seconds)` and map
  `TimeoutError` to a 504 ErrorResponse. Document the head-of-line behavior with H4.
- **Effort:** 15 min.

### M8. Response schema gaps for a frontend (details in Q9 below)
- **Where:** `schemas/response.py:264-272`, `445-446` and `460`.
- **What:**
  - `Visualization` is one model whose `type` decides whether `data` or `nodes`/`edges` apply.
    It is not a discriminated union, so OpenAPI cannot express it.
  - Warnings and assumptions are free strings: the cohort is a `"label: "` prefix and severity
    is a `"DEGRADED DATA:"` prefix.
  - `ErrorResponse.code` is a free `str` whose values come from three enums plus ad-hoc literals
    (`invalid_request`, `invalid_filter_value`, `upstream_*`).
  - Buckets with zero trials are omitted, so a grouped bar chart or time series has to infer
    zeros.
- **Fix:** make `code` a `StrEnum` that lists every code. Make warnings and assumptions
  `{code, cohort, message}` objects (keep `message` for humans). Use a discriminated union on
  `type`. Emit zero data for known keys in multi-cohort and temporal charts, or document that an
  absent datum means zero.
- **Effort:** 15 min for the code enum and the docs; 45+ min for structured warnings (after
  submission).

### M9. `estimated_count` is 0 where it does not apply, and it counts different things on different dimensions
- **Where:** `aggregate/group.py:118`, `aggregate/counted.py:85` and `schemas/response.py:107-111,434`.
- **What:** for phase, status, sponsor class and similar dimensions, `meta.estimated_count` is 0
  (example a, example i) and every Datum has `estimated_count: 0`. architecture.md:225 says it is
  None "when they do not apply", and decision 91's own reasoning ("0 would claim none are
  estimated") applies here too. On `enrollment_bucket` it counts estimated enrollments, while the
  Datum description says "via an estimated date".
- **Fix:** return None for dimensions without an estimate concept, either through a
  `DimensionSpec.estimate_kind` (`date` | `enrollment` | None) or by making the kind explicit, and
  fix the descriptions. The golden snapshots will need updating.
- **Effort:** 20 min.

### M10. The planner eval is stale and narrow
- **Where:** `docs/planner_eval.md`, which itself says "The tables above predate this case and the
  prompt change; the full set has not been re-run". `tests/eval/questions.yaml` has 16 questions.
- **What:** the AI/agent score (20%) rests on a 15-question run made before the ranking prompt
  change and before the network and node-filter changes. It has no adversarial items (injection,
  misspelled drug, non-English, multi-intent), and the two prompt-injection checks run only under
  `-m live`.
- **Fix:** re-run `scripts/eval_planner.py` on the current prompt for the default model, and add
  4–5 adversarial and misspelling questions.
- **Effort:** 15 min (the run is about 30 s per model).

---

## Low

- **L1. Doc and docstring drift.**
  - `config.py:1` and decisions.md:40 say settings use the "prefix CTVIZ_", but most settings
    accept unprefixed names first (`config.py:93-203`, `.env.example`).
  - architecture.md:137 says the cache is "on in development"; in fact `cache_enabled=True` is
    the default everywhere and there is no production switch.
  - decisions.md:83 still says "the planned exact count route", although it is implemented.
  - `schemas/plan.py:1` says "QueryPlan: the only thing the LLM produces"; the LLM produces
    `LlmPlanDraft`.
  - `app.py:4`: see H3.
  - architecture "only a /studies failure fails the request" vs `pipeline.py:452`: `fetch_data_timestamp`
    does not catch `ApiRequestError`, so a 4xx from `/version` fails the whole request with a 502.
  - Fix: one pass over these lines. Effort: 10 min.
- **L2. Dead plan fields echoed in every response.** `schemas/plan.py:99,101` (`metric`, which is
  constant, and `time_granularity`) are never set or read anywhere in `src/`, but appear in
  `meta.interpretation.plan`. A frontend engineer will ask what `time_granularity: null` means.
  Fix: remove `time_granularity` (and update the goldens), or document it. Effort: 5 min.
- **L3. Asserts used for control flow** (bandit B101): `pipeline.py:258,260,300,598,633` and
  `client.py:265`. Under `python -O` they disappear. Fix: explicit `if … raise`, or model
  `CohortRetrieval` as a tagged union. Effort: 10 min.
- **L4. The pagination loop has no page cap.** At `client.py:234-263`, a server that keeps
  returning a `nextPageToken` with empty `studies` loops forever (`test_empty_page_with_token_is_followed`
  covers one empty page). Fix: stop after `ceil(max_records / page_size) + k` pages and warn.
  Effort: 5 min.
- **L5. Silent coercions in normalize.**
  - `study.py:288-290`: a lead sponsor without a name is dropped as a whole, so a present `class`
    lands in `sponsor_class` Unknown without a warning.
  - `study.py:240`: blank country rows are skipped without a report.
  - Both are improbable, but both cut against invariants 3 and 4. Fix: keep the class when the
    name is missing; count skipped rows in `unrecognized_values`. Effort: 10 min.
- **L6. Misleading edge warning.** `network.py:330-339` says "no lead sponsor or no intervention
  MeSH term" even when the trial had terms and the node filter removed them (`without_edges` is
  counted after filtering, `network.py:132`). Fix: count the filtered-out case separately.
  Effort: 5 min.
- **L7. `meta.total_matched` is a sum over cohorts** (`spec/builder.py:106`). Example e reports
  4402, but the distinct trial count is 4229 (173 shared). The field description says "totalCount
  reported by the API". Fix: rename it or document it as a sum, and add `distinct_trials` when the
  overlap is known. Effort: 5 min.
- **L8. Coverage counts skipped records** (`client.py:273`). Coverage is "the share of matches the
  counts cover" (decision 92), but the numerator includes records that were skipped and therefore
  not counted. Fix: use `(retrieved - skipped) / total` in `cohort_stats`. Effort: 5 min.
- **L9. Cache hygiene.** `retrieval/cache.py`: expired rows are never deleted (the file grows
  without bound), there is no `timeout`/WAL setting, and `json.loads` of a 1.7 MB page runs on the
  event loop. Fix: `DELETE … WHERE stored_at < ?` on startup, plus
  `sqlite3.connect(path, timeout=5)` and `PRAGMA journal_mode=WAL`. Effort: 10 min.
- **L10. Structure.**
  - `pipeline.py` (661 lines) both orchestrates and composes every meta sentence. Moving the
    message builders (lines 310-353 and 525-661) to `pipeline_meta.py` would halve it.
  - `sample_problems` is computed twice (`pipeline.py:200,208`).
  - `retrieval_route.py` sits at the package root and imports `normalize_study` to read boundary
    dates, so retrieval routing depends on normalize. That is acceptable, but say so in
    architecture.md.
  - Defaults and bounds are declared twice (`NetworkLimits`/`DataQualityThresholds`/`CountRoutePolicy`
    vs `Settings`).
  - Effort: 20 min; can wait until after submission.
- **L11. LLM-authored free text reaches users.** `planner/plan.py:156-173` returns
  `unsupported_reason` and `clarification.options` verbatim in `ErrorResponse.message` and
  `suggestions`. They are not data, so invariant 1 holds, but a prompt-injected question can put
  arbitrary text in front of a user. Fix: length-cap the strings and document that a frontend must
  render them as text, not HTML. Effort: 5 min.

---

## Areas checked and found clean

- **Secrets:** `.env` is gitignored and never committed (`git ls-files` shows only `.env.example`).
  Keys are `SecretStr`, and the startup error does not echo them (`llm.py:411-416`). I did not
  open `.env`.
- **SQL:** every cache statement is parameterized (`cache.py:78-96`). The cache path comes from
  settings and never from user input.
- **Essie injection** of `"`, `()`, `[]` and uppercase `AND/OR/NOT` is rejected before any
  network or LLM call and is tested. The gap is M4.
- **Phase exclusivity and count-route partition:** the bucket clauses are mutually exclusive by
  construction (`registry.py:138-147`), so remainder 0 really does mean a partition. Fallback on a
  nonzero remainder and on sample contradictions is implemented and tested.
- **Dedup, top_n and overlap:** dedup by NCT ID across pages is correct. top_n keeps
  Unknown/Unrecognized and counts hidden trials correctly for both routes. Cohort overlap is
  correct.
- **Network weights** are sets of distinct NCT IDs (`network.py:135-138,272`).
- **LLM boundary:** the draft is schema-constrained, cohort labels and titles are built in code,
  explicit fields override the draft and each override is recorded, and validation applies equally
  to LLM drafts and structured input.

---

## Trace: example a, request → response

Request: `{"query": "How are pembrolizumab trials distributed across phases?", "drug": "pembrolizumab", "dimension": "phase"}`.

1. **Plan.** The LLM drafted cohort `{drug: pembrolizumab}` with dimension phase. The explicit
   fields match the draft, so `overrides` is empty. The label "pembrolizumab" was derived in
   code, and `planned_by` is "llm".
2. **Retrieval.** Phase has `api_count`, so the route uses `fetch_unless_above`. The total was
   2575, under the 10,000 cap, so the cohort was fetched. 2575 records were retrieved from cache,
   0 skipped, and the result reconciled.
3. **Aggregate.**

   | Early Phase 1 | Phase 1 | Phase 1/Phase 2 | Phase 2 | Phase 2/Phase 3 | Phase 3 | Phase 4 | Not Applicable | Unknown |
   |---|---|---|---|---|---|---|---|---|
   | 38 | 531 | 420 | 1148 | 36 | 311 | 15 | 25 | 51 |

   The buckets sum to 2575, which equals `total_matched`, `records_retrieved` and
   `cohorts[0].total_matched`. The buckets are exclusive (`buckets_overlap: false`). Each datum
   has `citation_count_total == count` (one phases citation per trial) and up to 20 citations;
   Phase 4 has exactly 15. The Unknown bucket cites `protocolSection.designModule.phases` with
   `value: null`. The warning "51 of 2575 trials have no phase" matches the bucket. The rules are
   applied consistently.
4. **Inconsistencies.** `estimated_count: 0` on a dimension with no estimate concept (M9).
   More important, the whole response is stale (H1): the current code sends name OR MeSH and gets
   2642 trials, 40/541/433/1163/41/314/16/27/67. That matches api_findings §17 and example e's
   pembrolizumab series exactly, which is good cross-evidence that the current code is right and
   example a is old.

Examples e, h and i were also checked:
- **e:** each cohort's buckets sum to its total, the 173-trial overlap is reported with an
  assumption, and `total_matched` 4402 is a sum (L7).
- **h:** no dangling edges, every edge has weight ≥ 2, `edge_citations_reduced` applies the cap of
  2, and the caps and exclusions are all in meta. The hub undercount is M1.
- **i:** count route with 10 requests (1 + 9), remainder 0, 90 sampled records, buckets summing to
  16,913, and `coverage` 1.0.

---

## Q9: could a renderer be built from the schema alone?

For bar, grouped bar and histogram charts: mostly yes. For time series and network graphs: yes,
but only after reading the code. I had to read code to understand these fields:
- `Visualization.data` vs `nodes`/`edges`: which one is populated depends on `type` (only a
  docstring says so).
- `encoding.x.field` is `"label"` for nominal axes and `"key"` for temporal ones. Temporal keys are
  strings (`"2015"`, `"2026-09"`), mixed with `"Unknown"` in the same array. Vega-Lite parses
  `"2015"` as UTC midnight and `timeUnit: "year"` as local time, so a US-timezone renderer shows
  the year as 2014 unless it uses `utcyear`. Either emit `"time_unit": "utcyear"` or document it.
- Absent datum means zero trials (zero buckets are dropped in `counted.py:62`, and `group_by`
  never creates them). A renderer needs that to fill gaps in a grouped bar chart or line chart.
- `citation_count_total`: trials vs citations, see M2.
- `estimated_count`: date vs enrollment vs not applicable, see M9.
- `Datum.cohort` is set even for a single cohort, while `encoding.color` is null.
- Node `type: "drug_class"` keeps the `drug:` id prefix (documented, but surprising).
- Edge id `"<source>--<target>"`, where the source is the lexically smaller id
  (`network.py:208-211`). Edges are undirected, so `source`/`target` order carries no meaning.
- `meta.warnings[0]` being "DEGRADED DATA:…" is the only machine signal for degraded data apart
  from `data_quality`. Per-cohort messages can only be told apart by string prefix.
- `meta.interpretation.plan.time_granularity` and `metric` (L2).
- `ErrorResponse.code`: the full set of values can only be found by reading `app.py`.

---

## Q10: what to fix in the next 60 minutes, in order

1. **H1** Regenerate all examples and add the example-schema drift test (25 min).
2. **H3** Add a catch-all 500 ErrorResponse handler and fix the negative-enrollment crash with a
   test (15 min).
3. **H2** README (10 min).
4. **M4 + M3** Reject `\` and control characters, and add `max_length` on query, filters and lists
   (10 min).
5. **H4 (docs only) + L1** Document the single-worker limit and fix the drift lines (10 min,
   which runs about 10 min past the hour).

Next, if there is time: M5 (cap skipped-record warnings), M1 (hub undercount assumption), M10
(re-run the eval).

---

## Test quality notes

- `tests/test_truncation.py:73-129` tests truncation on `Dimension.PHASE`, which is countable.
  The tests reach the truncated fetch only because the count route falls back on a nonsensical
  remainder: every bucket mock returns the full total. They pass, but they exercise the fallback
  path by accident and never assert the fallback warning. Use `Dimension.COUNTRY` (fetch-only)
  instead.
- `tests/test_adversarial.py:262`: `all(datum.count == 0 for datum in data)` is vacuous on empty
  data. Assert `data == []`.
- `tests/test_concurrency.py:20-23,72-74` measures real wall-clock gaps with 20 ms slack, which can
  flake on a loaded CI runner. An injected clock (as in `test_retrieval_client.py`) would be
  deterministic.
- **Behaviors with no test:**
  - The boundary-date fallback (`retrieval_route.py:98-101,160-166`).
  - A negative or float enrollment.
  - A backslash in a filter.
  - Any unexpected exception → ErrorResponse (no catch-all exists to test).
  - A `/version` 4xx.
  - SQLite contention.
  - A refetch after sample contradictions on a cohort larger than the cap (the fallback fetch is
    then truncated, which is the double-fallback path).
  - `examples/` against the current schema (H1).
- Otherwise the suite is strong: 651 offline tests, goldens, poison records, count-route
  reconciliation (live), injected clocks for pacing and backoff, and fakes for the LLM chain.

---

## Tool output

To run them, `bandit` and `pip-audit` were added as dev dependencies (`uv add --dev bandit pip-audit`).
That changed `pyproject.toml` (`bandit>=1.9.4`, `pip-audit>=2.10.1`) and `uv.lock`. Reason: a
static security scan and a dependency CVE audit, both dev-only.

**`uv run bandit -r src/ -q`** (bandit 1.9.4): 6 issues, all Low severity / High confidence, all
`B101:assert_used`. Medium 0, High 0. 6,141 lines scanned, 0 `#nosec`.

| Location | Code |
|---|---|
| src/ctviz/pipeline.py:258 | `assert retrieval.fetch is not None` |
| src/ctviz/pipeline.py:260 | `assert dimension is not None` |
| src/ctviz/pipeline.py:300 | `assert retrieval.fetch is not None` |
| src/ctviz/pipeline.py:598 | `assert fetch is not None` |
| src/ctviz/pipeline.py:633 | `assert plan.dimension is not None` |
| src/ctviz/retrieval/client.py:265 | `assert total_count is not None` |

See L3. Nothing was fixed.

**`uv run pip-audit`** (pip-audit 2.10.1): "No known vulnerabilities found". One package was
skipped: `ctviz (0.1.0)`, "Dependency not found on PyPI and could not be audited" (the project
itself, which is expected).
