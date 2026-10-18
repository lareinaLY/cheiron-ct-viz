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
- the chart hint is incompatible with the data.

It reuses the chart compatibility rules from `spec/chart.py`, so the validator and
chart selection cannot disagree (invariant 7).

**retrieval** (`retrieval/`, not built yet apart from `fields.py`). This is the only
stage that does network I/O. It turns filters into API params, always projects with
`STUDY_FIELDS`, paces requests and retries 429s, resends frozen params on every page,
and caps a full fetch at 10,000 records. It returns the raw studies plus `totalCount`
and `dataTimestamp`, so the response can reconcile the retrieved count against
`totalCount` and report truncation and coverage (invariants 3, 6).

**normalize** (`normalize/study.py`). `normalize_study` is a pure function from one raw
study dict to a `TrialRecord` (`schemas/trial.py`). It derives the phase bucket and
date precision, trims and deduplicates countries, and keeps intervention names
verbatim. It also takes the study-level intervention MeSH list. Each record carries
`source_paths`, plus the indices and raw values needed to cite the exact source value
later. Uninterpretable values raise `SourceRecordError` rather than being guessed, so
the caller can drop the record and report it.

**aggregate** (`aggregate/`). These are pure functions over `TrialRecord`s:
- `group_by` returns a `GroupResult`: buckets with counts, estimated counts, an
  incomplete-period flag, capped citations plus the true citation total, an overlap
  flag, and `assumptions`/`warnings`.
- `build_network` returns a `Graph` (nodes, weighted edges, citations).

Missing values go to an explicit `Unknown` bucket (invariant 4). Every bucket, node
and edge carries citations (invariant 2). Unknowns, estimated values, in-progress
periods and overlap are all reported as warnings or assumptions (invariant 3).

**spec** (`spec/`). This stage turns aggregation results into a
`VisualizationResponse` (`schemas/response.py`). So far only `select_chart` is built:
it chooses the chart type from the dimension's kind and the number of cohorts, and
honors a compatible hint. Building the `Meta` block (count unit, filters, coverage,
assumptions and warnings copied from the aggregate result) is the next step
(invariant 5).

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
| 6. Count reconciles with totalCount | retrieval, then `Meta.total_matched` / `records_retrieved` / `coverage` |
| 7. Structured errors over fabricated charts | `PlanValidationError`, `SourceRecordError` |
