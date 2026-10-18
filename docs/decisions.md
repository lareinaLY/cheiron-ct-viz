# Design decisions

One line per decision: decision — reason — rejected alternative.

- uv + pyproject.toml for packaging — reproducible `uv.lock` and fast installs; `pip install -e .` still works from pyproject as a fallback — rejected: requirements.txt + pip-tools, Poetry.
- FastAPI + Pydantic v2 — the same models validate stage boundaries and generate the OpenAPI docs, so schemas double as API documentation — rejected: Flask + marshmallow, hand-written JSON schemas.
- No agent framework; a fixed plan → validate → execute pipeline — explicit control flow, the LLM only emits a QueryPlan, smaller hallucination surface, easy to test deterministically — rejected: LangChain/LlamaIndex agents with tool calling loops.
- No database server — data comes live from the ClinicalTrials.gov API and there is no persistent business data; a local cache (SQLite/file) sits behind an interface, with Postgres/AACT as the production path for heavy aggregation — rejected: running Postgres/AACT from day one.
