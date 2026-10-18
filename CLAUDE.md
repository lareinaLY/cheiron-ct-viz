# CLAUDE.md

Backend service: natural-language clinical-trial questions → structured visualization specs, backed by the ClinicalTrials.gov API v2. Design: docs/architecture.md. API facts: docs/api_findings.md.

## Invariants (violating any of these is a bug)
1. The LLM only translates a question into a QueryPlan. It never produces numbers, counts, labels for data points, or any data value. Retrieval, aggregation and spec building are deterministic code.
2. Every visualized datum carries citations: nct_id + source field path + exact source value.
3. Nothing is silent. Any cap, truncation, dropped record, missing value, bucket overlap, or assumption is reported in the response `meta`.
4. Missing is not zero. Never coerce unknown values into 0 or into another bucket; use an explicit "Unknown" bucket or report them in meta.
5. Every response states its counting unit (`meta.count_unit`).
6. When fetching a full result set, the number of records retrieved must reconcile with the API's totalCount; any mismatch is reported in meta.
7. If a question can't be answered faithfully, return a structured error or an empty result with an explanation. Never fabricate a chart.

## Architecture boundaries
- Pipeline: planner → validator → retrieval → normalize → aggregate → spec. One job per stage.
- Only `retrieval/` performs network I/O to ClinicalTrials.gov. Only `planner/` calls the LLM.
- `normalize/` and `aggregate/` are pure functions over typed records: no I/O, no LLM.
- New query types are added by registering a dimension or relation in the registry, never by branching on question text.
- All data crossing a stage boundary or the HTTP API is a Pydantic model. Prefer enums over free strings.
- No database server. Caching is local (SQLite/file) behind an interface.

## Code style
- Simple and explicit over clever. No premature abstraction; add a layer only when there is a second real use.
- Small functions with one responsibility. Descriptive names; no abbreviations except well-known ones (nct_id, api, llm).
- Full type hints on all functions. Formatting and linting with ruff.
- All code, comments, docstrings, logs and docs are in English.
- Docstrings on every public function/class: what it does, and why when non-obvious. Comments explain why, not what.
- No commented-out code, no print debugging (use logging), no TODOs without context.
- Raise specific exceptions with actionable messages; never swallow exceptions silently.

## Testing
- Unit tests run offline against fixtures in tests/fixtures/. Network tests are marked `@pytest.mark.live`.
- Every aggregation/normalization rule has a test, including edge cases (multi-phase, partial dates, missing fields).
- For normalize/, aggregate/, the registries, chart-selection rules and the plan validator: write tests first, confirm they fail on assertions, then implement.
- Never modify, weaken, or delete an existing test to make it pass. If a test looks wrong, stop and explain why.
- Never special-case test inputs in implementation code.

## Workflow
- Do NOT run `git commit` or `git push`. The developer commits manually. At the end of each task, propose how to split the changes into commits (files per commit + conventional message: feat/fix/test/docs/chore/refactor).
- Schema changes update docs and examples in the same change.
- Record each significant design decision in docs/decisions.md as one line: decision — reason — rejected alternative.
- Don't add a dependency without stating why.
- Never read, print, or commit `.env` or any secret.
- If the spec or a data quirk is ambiguous, stop and ask instead of guessing.