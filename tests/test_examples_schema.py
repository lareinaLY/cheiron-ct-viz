"""The committed examples in examples/ match the current response schema.

The examples are documentation that readers copy from, so a schema change that is not
followed by regenerating them would silently mislead. Pydantic fills missing fields with
defaults when loading, so a stale example would still load; the key checks catch that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ctviz.retrieval.query_builder import SEMANTICS
from ctviz.schemas.response import CohortStats, Meta, VisualizationResponse

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
RESPONSE_PATHS = sorted(EXAMPLES_DIR.glob("*.response.json"))


def load_raw(path: Path) -> dict[str, Any]:
    """The example response as plain JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_examples_exist() -> None:
    assert len(RESPONSE_PATHS) == 12


@pytest.mark.parametrize("path", RESPONSE_PATHS, ids=lambda path: path.name)
class TestExampleResponse:
    def test_loads_into_visualization_response(self, path: Path) -> None:
        VisualizationResponse.model_validate(load_raw(path))

    def test_every_meta_key_is_present_and_none_is_unknown(self, path: Path) -> None:
        assert set(load_raw(path)["meta"]) == set(Meta.model_fields)

    def test_every_cohort_key_is_present_and_none_is_unknown(self, path: Path) -> None:
        for cohort in load_raw(path)["meta"]["cohorts"]:
            assert set(cohort) == set(CohortStats.model_fields)

    def test_search_semantics_are_ones_the_query_builder_emits(self, path: Path) -> None:
        meta = load_raw(path)["meta"]
        for name, text in meta["search_semantics"].items():
            key = f"drug_{meta['match_mode']}" if name == "drug" else name
            assert text == SEMANTICS[key], name
