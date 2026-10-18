# Architecture

A question goes through six stages. Each stage does one job, and every boundary is a
Pydantic model.

```
question (str)
   │
   ▼
┌──────────┐  QueryPlan   ┌───────────┐  QueryPlan   ┌───────────┐  raw study dicts
│ planner  │ ───────────▶ │ validator │ ───────────▶ │ retrieval │ ─────────────────┐
│  (LLM)   │              │           │              │ (network) │  + totalCount,   │
└──────────┘              └───────────┘              └───────────┘    dataTimestamp │
                                                                                  ▼
┌──────────┐ GroupResult / Graph ┌───────────┐  list[TrialRecord]   ┌───────────┐
│   spec   │ ◀────────────────── │ aggregate │ ◀─────────────────── │ normalize │
└──────────┘                     └───────────┘                      └───────────┘
   │                                   ▲
   ▼                                   │ dispatches through
VisualizationResponse         aggregate/registry.py (DIMENSIONS, RELATIONS)
```

## Stages

**planner** (`planner/`, not built yet). This is the only stage that calls the LLM. It
translates the question into a `QueryPlan` (`schemas/plan.py`): cohorts with filters, one
dimension or one relation, an optional chart hint, and the match mode. The plan
never holds data values (invariant 1).

**validator** (`planner/validation.py`). `validate_plan` rejects plans that cannot be
answered faithfully, raising `PlanValidationError` with a `PlanErrorReason`:
- neither or both of dimension/relation is set;
- a relation is combined with several cohorts;
- the chart hint is incompatible with the data;
- `top_n` is below 1, or combined with a relation (networks have no buckets to trim);
- a cohort's `start_year_from` is after its `start_year_to`.

It reuses the chart compatibility rules from `spec/chart.py`, so the validator and
chart selection cannot disagree (invariant 7).

**retrieval** (`retrieval/`). This is the only stage that does network I/O.
- `query_builder.py` (pure) turns one cohort's `Filters` into `/studies` params. The
  drug goes to `filter.advanced=AREA[InterventionName]"<drug>"` (always quoted) or, in
  `broad` mode, to `query.intr`. Condition, sponsor and country go raw to `query.cond`,
  `query.spons` and `query.locn`; values containing Essie syntax (`"`, `()`, `[]`,
  standalone `AND`/`OR`/`NOT`) are rejected with `InvalidFilterError`. Phases, start
  years and the drug clause are AND-joined in `filter.advanced`; statuses use
  `filter.overallStatus`. `search_semantics` describes what each filter actually
  matches, for `meta.search_semantics`.
- `client.py` (`CtGovClient`) always projects with `STUDY_FIELDS`, sends identical
  params on every page (only `pageToken` changes), pages at min(1000, cap), paces
  requests with a one-token bucket (1 request / 1.2 s), retries 429, 5xx and network
  errors with exponential backoff plus jitter, and fails fast on other 4xx with the
  API's error text. A full fetch stops at `max_records` (10,000). It returns a
  `FetchResult` (`schemas/retrieval.py`): studies deduplicated by NCT ID, `total_count`,
  `truncated`, `coverage`, and `reconciled`. A count that does not match totalCount is
  flagged and reported in meta, not raised (invariant 6). `/version`'s dataTimestamp is
  reused for an hour.
- `cache.py` stores successful GET bodies in SQLite under `.cache/`, keyed by URL plus
  sorted params, with a TTL (6 h by default; on in development, `NoCache` in tests).
  When page 1 comes from the cache, later pages must too; on a gap the fetch restarts
  live, because page tokens from different fetches cannot be mixed.

**normalize** (`normalize/study.py`). `normalize_study` is a pure function from one raw
study dict to a `TrialRecord` (`schemas/trial.py`). It derives the phase bucket and
date precision, trims and deduplicates countries, and keeps intervention names
verbatim. It also takes the study-level intervention MeSH list. Each record carries
`source_paths`, plus the indices and raw values needed to cite the exact source value
later. Uninterpretable values raise `SourceRecordError` rather than being guessed, so
the caller can drop the record and report it.

**aggregate** (`aggregate/`). These are pure functions over `TrialRecord`s:
- `group_by` returns a `GroupResult`: buckets with counts, estimated and
  unknown-date-type counts (also totalled on the result), an
  incomplete-period flag, capped citations plus the true citation total, an overlap
  flag, and `assumptions`/`warnings`.
- `build_network` returns a `Graph` (nodes, weighted edges, citations).
- `group_cohorts` runs `group_by` per cohort and returns `GroupedCohorts`: one result per
  cohort on a shared `key_order` (the dimension's order over the union of trials). With
  `top_n`, it keeps the N known keys with the most trials plus Unknown, and records the
  omitted keys and, per cohort, the trials that appear only in omitted buckets.

Missing values go to an explicit `Unknown` bucket (invariant 4). Every bucket, node
and edge carries citations (invariant 2). Unknowns, estimated values, in-progress
periods and overlap are all reported as warnings or assumptions (invariant 3).

**spec** (`spec/`). `build_response` (`spec/builder.py`) turns a `GroupedCohorts` or
`Graph` into a `VisualizationResponse` (`schemas/response.py`). It is pure, and is
checked against golden snapshots in `tests/golden/`.
- `select_chart` chooses the chart type from the dimension's kind and the number of
  cohorts, and honors a compatible hint.
- Grouped results become `data` (one `Datum` per bucket and cohort). Encoding uses
  Vega-Lite-style channels (`field`, `type`: nominal | temporal | quantitative, `sort`,
  `time_unit`), and several cohorts are colored by `cohort`.
- Graphs become Cytoscape-style `nodes`/`edges` elements, with the payload under `data`.
- Titles are derived from the plan only (e.g. "Trials by phase for pembrolizumab").
  Titles and axis titles use the dimension's `title_label`, which states the granularity
  of temporal dimensions ("start year"); warnings keep using `label` ("start date").
- Every temporal datum carries `estimated_count` and `unknown_date_type_count` (dates
  with no ACTUAL/ESTIMATED type), so a renderer can style recent or uncertain periods
  differently. The totals are in `meta.estimated_count` and `meta.unknown_date_type_count`
  (None when they do not apply).
- `Meta` carries `interpretation` (the question, the executed plan and `planned_by`),
  `count_unit`, `search_semantics`, retrieval totals plus per-cohort `cohorts` stats,
  `top_n`, `data_timestamp`, `timing_ms`, and every assumption and warning (invariants 3, 5).

**pipeline** (`pipeline.py`). `run(plan, question, client)` calls the stages in order and
times each one. Filters are built during validation, so bad values fail before any
network call. A study that raises `SourceRecordError` is skipped, counted in
`cohorts[].records_skipped` and reported by NCT ID in `meta.warnings`, never fatal.
Truncation, reconciliation failures, duplicates and cache use also go into meta.

**api** (`api/`). `POST /query` takes `QueryRequest`: `query` (echoed in
`meta.interpretation`, not interpreted until the LLM planner exists) plus structured
fields that map to one cohort and a dimension or relation. Failures return
`ErrorResponse` (`code`, `message`, `suggestions`): 422 for invalid requests, plans and
filter values; 502 when the API fails or rejects the query; 503 when it keeps
rate-limiting. `GET /health` is a liveness probe. Run the app with
`uv run uvicorn ctviz.api.app:app`. Example requests and real responses are in `examples/`.

## Registry

`aggregate/registry.py` declares every dimension and relation.
- A `DimensionSpec` gives: how to extract values from a record, the citation path
  (`source_field` + `leaf`), whether a trial can fall into several buckets, the bucket
  order, the period key for in-progress flags, its kind (categorical, temporal or
  binned), and fixed assumptions.
- A `RelationSpec` pairs two dimensions with node types.

`group_by`, `build_network` and `select_chart` dispatch through the registry only. To
add a new query type, add a registry entry and a `Dimension`/`Relation` enum value; no
other code should branch on dimension names.

## Where invariants are enforced

| Invariant | Enforced in |
|---|---|
| 1. LLM only produces a plan | `planner/` boundary; `QueryPlan` has no data fields (`extra="forbid"`) |
| 2. Citations on every datum | `aggregate/registry.py` extractors and `cite`; `Bucket`, `NetworkNode`, `NetworkEdge` |
| 3. Nothing silent | `GroupResult`/`Graph` `assumptions` and `warnings`, copied into `Meta` |
| 4. Missing is not zero | `UNKNOWN_BUCKET_KEY`, kept separate from the API's own `UNKNOWN` |
| 5. Count unit stated | `Meta.count_unit` |
| 6. Count reconciles with totalCount | `CtGovClient.fetch_all` (`FetchResult.reconciled`), then `Meta.cohorts[]` and a meta warning |
| 7. Structured errors over fabricated charts | `PlanValidationError`, `InvalidFilterError`, `SourceRecordError`, `ErrorResponse` |
