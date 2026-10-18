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

**planner** (`planner/`). This is the only stage that calls the LLM. It translates the
question into a `QueryPlan` (`schemas/plan.py`): cohorts with filters, one dimension or
one relation, an optional chart hint, `top_n` and the match mode. The plan never holds
data values (invariant 1). `plan(request, llm, today)` (`planner/plan.py`) returns an
`Interpretation` (`schemas/response.py`):
- With an LLM (a key for at least one provider in `LLM_FALLBACK_CHAIN`): `prompt.py`
  builds the messages and `llm.py` returns an `LlmPlanDraft`: filter sets per cohort,
  the grouping, `ranking_intent`, `unsupported_reason` and `clarification`.
  `FallbackChain` tries the configured providers in order (default
  `openai:gpt-4.1-mini`; optionally `deepseek:deepseek-chat`). Each is an
  `OpenAICompatibleProvider` with one SDK retry on transient errors, asked in its
  recorded response mode: OpenAI with a strict JSON schema (`chat.completions.parse`),
  DeepSeek in `json_object` mode with the schema embedded in the system prompt (it
  rejects strict `json_schema`). The chain moves to the next provider only after an
  infrastructure failure (timeout, connection error, 429, 5xx); any other failure is
  final, and validation rejections stay on the retry-with-error path below. A provider
  without a key is skipped at startup with a log line. Every LLM call logs one line
  (`llm_call provider= model= latency_ms= input_tokens= output_tokens= outcome=`, with
  outcome ok, retry, fallback or failed). A set
  `unsupported_reason` or `clarification` becomes a `PlannerError`
  (`unsupported_question`, `clarification_needed` with the readings as suggestions).
  Otherwise `merge_draft` applies the request's explicit fields on top: an explicit
  filter replaces the LLM's value in every cohort; an explicit dimension clears the
  LLM's relation and vice versa. Each replaced value is an `overrides` entry. Cohort
  labels are derived from the merged filters, never taken from the LLM. The plan is then
  validated (`validate_plan` plus the query builder's filter checks). A rejected draft is
  retried once with the error appended to the conversation; a second rejection is a
  `planning_failed` error. When the draft flags `ranking_intent` ("which countries have
  the most ...") and neither the question nor the request gives `top_n`, the planner
  sets `top_n` to `DEFAULT_TOP_N_FOR_RANKING` (15) and records it in `ranking_top_n`;
  the LLM never picks the number, and relations are never trimmed. `planned_by="llm"`,
  with `model` (primary of the chain), `provider`, `model_used` (as reported by the
  provider), `response_mode`, `fallback_reason`, `draft`, `overrides`,
  `rejected_attempts`, `ranking_top_n` and `planning_ms`.
- Without an LLM: a request with structured fields is planned from them alone
  (`planned_by="rules"`); a request with `query` only is an `llm_not_configured` error.
- The prompt is rendered from the registry (dimension and relation descriptions),
  `spec/chart.py` (chart descriptions and compatibility) and the API enums (phases,
  statuses), so prompt and code cannot drift. It states the rules: copy names verbatim,
  never add a filter that was not stated, decline or ask instead of guessing, plain
  filter values only. It ends with 8 few-shot `LlmPlanDraft` examples, one per query
  class plus one decline and one clarification, and each of them validates in a test.
- `tests/eval/questions.yaml` and `scripts/eval_planner.py` score the planner on real
  models; results are in `docs/planner_eval.md`.

**validator** (`planner/validation.py`). `validate_plan` rejects plans that cannot be
answered faithfully, raising `PlanValidationError` with a `PlanErrorReason`:
- neither or both of dimension/relation is set;
- a relation is combined with several cohorts;
- the chart hint is incompatible with the data;
- `top_n` is below 1, or combined with a relation (networks have no buckets to trim);
- a cohort's `start_year_from` is after its `start_year_to`;
- two cohorts share a label or identical filters (results are keyed by label, and
  identical cohorts compare nothing; e.g. an explicit `drug` over an LLM comparison).

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
  `FetchResult` (`schemas/retrieval.py`): studies as plain decoded JSON (never parsed into
  a model, so one malformed study reaches normalize instead of failing the fetch; a
  `studies` value that is not a list is an `UpstreamError`), deduplicated by NCT ID,
  `total_count`, `truncated`, `coverage`, and `reconciled`. A count that does not match totalCount is
  flagged and reported in meta, not raised (invariant 6). `/version`'s dataTimestamp is
  reused for an hour; a missing or malformed one is logged and returned as None (not
  reused). When /version gives no valid timestamp, or fails after its retries (5xx,
  network error, 429, non-object body), the pipeline sets `meta.data_timestamp` to null
  with a warning naming the cause; only a /studies failure fails the request.
- `enums.py` runs the startup drift check: `CtGovClient.fetch_enums` (cached) gets
  `/studies/enums`, `enum_drift` compares Phase, Status, StudyType, AgencyClass and
  InterventionType with our enums, and `check_enum_drift` logs one WARNING line listing
  new values (or enum types the API no longer lists). Any failure is logged and ignored.
- `cache.py` stores successful GET bodies in SQLite under `.cache/`, keyed by URL plus
  sorted params, with a TTL (6 h by default; on in development, `NoCache` in tests).
  When page 1 comes from the cache, later pages must too; on a gap the fetch restarts
  live, because page tokens from different fetches cannot be mixed.

**normalize** (`normalize/`). `normalize_study` (`study.py`) is a pure function from one
raw study (plain JSON) to a `TrialRecord` (`schemas/trial.py`). It derives the phase bucket and
date precision, trims and deduplicates countries, and keeps intervention names
verbatim. It also takes the study-level intervention MeSH list. Each record carries
`source_paths`, plus the indices and raw values needed to cite the exact source value
later. Every value is read through a typed path lookup (`StudyReader`); unknown sections
and keys are ignored. Failures fall into two classes:
- structural (not an object, no protocolSection or NCT ID, a value of the wrong JSON
  type, a nameless intervention or MeSH entry): `SourceRecordError`, so the caller skips
  the record and reports it (NCT ID, or "unknown");
- value-domain (a string outside our enum for phase, status, study type, sponsor or
  collaborator class, intervention type or enrollment type; a date in an unknown format;
  an unknown date type): the record is kept, the field holds `UnrecognizedValue(raw)`
  (dates: precision or type "unrecognized", raw string kept), and each occurrence is
  listed in `TrialRecord.unrecognized_values`. `unrecognized_warnings`
  (`unrecognized.py`) turns them into one meta warning per distinct value, with its
  trial count, example NCT IDs and what happens to those trials.

**aggregate** (`aggregate/`). These are pure functions over `TrialRecord`s:
- `group_by` returns a `GroupResult`: buckets with counts, estimated and
  unknown-date-type counts (also totalled on the result), an
  incomplete-period flag, a future-period flag (`is_future`: periods after `as_of`,
  reported in one warning as projections), capped citations plus the true citation total, an overlap
  flag, and `assumptions`/`warnings`.
- `build_network` returns a `Graph` (nodes, weighted edges, citations, and a
  `NetworkSummary`). Node size and edge weight are counts of distinct trials. Values on the
  relation's deny-list (`RelationSpec.excluded`, by MeSH id) never become nodes; each
  excluded term is listed with its trial count. With readability caps (off by default;
  the pipeline passes `MAX_NETWORK_NODES`=50 and `MIN_EDGE_WEIGHT`=2 from settings), edges
  below the weight threshold are dropped, then the nodes with the highest weighted degree
  (sum of the remaining incident edge weights) are kept (ties: size, then label) with
  only the edges between them. Node sizes are not recomputed. Omitted nodes and edges,
  and the trials left without a shown edge or node, are counted in the summary and
  stated in a warning.
- `group_cohorts` runs `group_by` per cohort and returns `GroupedCohorts`: one result per
  cohort on a shared `key_order` (the dimension's order over the union of trials). With
  `top_n`, it keeps the N known keys with the most trials plus Unknown, and records the
  omitted keys and, per cohort, the trials that appear only in omitted buckets.

Values outside a known enum go to an `Unrecognized` bucket (`is_unrecognized`), sorted
after known values and before Unknown, and never removed by `top_n`; a date in an
unrecognized format has no period and goes to Unknown on temporal dimensions (citing
the raw string), and an unrecognized date type counts as untyped.
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
  The graph's `NetworkSummary` becomes `meta.network`. When the serialized network
  response is larger than `NETWORK_MAX_RESPONSE_BYTES` (1 MB), `fit_network_budget` cuts
  each edge's citations to `NETWORK_REDUCED_EDGE_CITATION_CAP` (2: both endpoints in one
  trial) instead of dropping edges, keeps `citation_count_total`, and states the sizes
  before and after in a warning.
- Titles are derived from the plan only (e.g. "Trials by phase for pembrolizumab").
  Titles and axis titles use the dimension's `title_label`, which states the granularity
  of temporal dimensions ("start year"); warnings keep using `label` ("start date").
- Every temporal datum carries `is_future` (its period starts after the as-of date, so
  it is a projection of planned starts), `estimated_count` and `unknown_date_type_count` (dates
  with no ACTUAL/ESTIMATED type), so a renderer can style recent or uncertain periods
  differently. The totals are in `meta.estimated_count` and `meta.unknown_date_type_count`
  (None when they do not apply).
- Every datum carries `is_unknown` (missing) and `is_unrecognized` (a value the API sent
  that we do not know), so a renderer can show both apart from real values.
- `Meta` carries `interpretation` (the question, the executed plan, `planned_by` and,
  for LLM plans, the model, draft, overrides and rejected drafts),
  `count_unit`, `search_semantics`, retrieval totals plus per-cohort `cohorts` stats,
  `data_quality` ("normal" or "degraded"), `top_n`, `network` (exclusions, caps and
  citation reduction; networks only),
  `data_timestamp`, `timing_ms`, and every assumption and warning (invariants 3, 5).

**pipeline** (`pipeline.py`). `execute(interpretation, client)` calls the stages in order
and times each one (planning time is `interpretation.planning_ms`). `run(plan, question,
client)` executes a plan supplied directly by code (`planned_by="structured_fields"`).
How the plan was made (who read the question and through which provider, a provider
fallback, each explicit override, each rejected draft, a ranking default for `top_n`
with the number of omitted buckets) is stated in `meta.assumptions`. Filters are built during validation, so bad values fail before any
network call. A study that raises `SourceRecordError` is skipped, counted in
`cohorts[].records_skipped` and reported by NCT ID in `meta.warnings`. Each unrecognized
value gets one warning. Truncation, reconciliation failures, duplicates and cache use
also go into meta. After normalize, each cohort's skip ratio (skipped / retrieved) is
logged (`normalize cohort= retrieved= skipped= skip_ratio= unrecognized= band=`) and
classified with `DataQualityThresholds` (`DEGRADED_SKIP_RATIO` 0.05, `REFUSE_SKIP_RATIO`
0.5, `MIN_SKIPPED_FOR_DEGRADED` 20): below either threshold, or with fewer than 20 skipped,
the result is normal; at the degraded ratio `meta.data_quality` is "degraded" and a
`DEGRADED DATA:` warning comes first; at the refuse ratio in any cohort,
`UpstreamDataFormatError` is raised instead of returning a chart that could mislead.

**api** (`api/`). `POST /query` takes `QueryRequest` (`schemas/request.py`): `query`,
which alone is enough when the LLM planner is configured, plus optional structured
fields that override the LLM's reading (without an LLM they map to one cohort and a
dimension or relation). The app builds the LLM fallback chain at startup, so a
malformed chain or API key stops the server with a clear message. It also starts the enum
drift check as a background task (`CTVIZ_ENUM_DRIFT_CHECK`, on by default), so a slow or
failing `/studies/enums` never delays startup; the task is cancelled at shutdown. Failures return
`ErrorResponse` (`code`, `message`, `suggestions`): 422 for invalid requests, plans and
filter values, and for unsupported, ambiguous or unplannable questions; 502 when the API
or the LLM provider fails, the API rejects the query, or too many retrieved records are
unreadable (`upstream_data_format_changed`); 503 when the API keeps
rate-limiting. The network caps come from settings (`Settings.network_limits()`) and are
passed to `pipeline.execute`. `GET /health` is a liveness probe. Run the app with
`uv run uvicorn ctviz.api.app:app`. Example requests and real responses are in `examples/`.

## Registry

`aggregate/registry.py` declares every dimension and relation.
- A `DimensionSpec` gives: how to extract values from a record, the citation path
  (`source_field` + `leaf`), whether a trial can fall into several buckets, the bucket
  order, the period key for in-progress flags, its kind (categorical, temporal or
  binned), fixed assumptions, and a `description` rendered into the planner prompt.
- A `RelationSpec` pairs two dimensions with node types, plus a `description` and
  `excluded`: per node type, the value keys that are never drawn. All three relations
  exclude `NETWORK_EXCLUDED_MESH`, a short list of exact MeSH ids observed on live data:
  comparators without active treatment (Standard of Care, Observation, Watchful Waiting,
  Saline Solution as the placebo mapping) and derived terms that are not interventions
  (Taxes, Single Person, Automobiles, Congresses as Topic, Solutions).

`group_by`, `build_network` and `select_chart` dispatch through the registry only. To
add a new query type, add a registry entry and a `Dimension`/`Relation` enum value; no
other code should branch on dimension names.

## Where invariants are enforced

| Invariant | Enforced in |
|---|---|
| 1. LLM only produces a plan | `planner/` boundary; the LLM returns an `LlmPlanDraft` (filters and a grouping only); `QueryPlan` has no data fields (`extra="forbid"`); cohort labels are derived in code |
| 2. Citations on every datum | `aggregate/registry.py` extractors and `cite`; `Bucket`, `NetworkNode`, `NetworkEdge` |
| 3. Nothing silent | `GroupResult`/`Graph` `assumptions` and `warnings`, copied into `Meta`; `Meta.network` for network exclusions, caps and citation cuts; skipped records and unrecognized values in `pipeline.retrieval_warnings`; `Meta.data_quality` |
| 4. Missing is not zero | `UNKNOWN_BUCKET_KEY`, kept separate from the API's own `UNKNOWN` and from `UNRECOGNIZED_BUCKET_KEY` (a present value outside our enums) |
| 5. Count unit stated | `Meta.count_unit` |
| 6. Count reconciles with totalCount | `CtGovClient.fetch_all` (`FetchResult.reconciled`), then `Meta.cohorts[]` and a meta warning |
| 7. Structured errors over fabricated charts | `PlanValidationError`, `InvalidFilterError`, `SourceRecordError`, `UpstreamDataFormatError` (refuse threshold), `ErrorResponse` |
