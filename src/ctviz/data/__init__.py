"""Static reference data packaged with ctviz.

`mesh_trees.json` maps each MeSH descriptor's preferred name to its tree numbers. It is
derived from NLM's `mtrees<year>.bin` by scripts/fetch_mesh_trees.py (source, year and
license: docs/api_findings.md §18) and is read once, like a constant; nothing here touches
the network.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

MESH_TREES_RELEASE = "NLM MeSH 2025"


@cache
def load_mesh_trees() -> dict[str, list[str]]:
    """The packaged {preferred_name: [tree_numbers]} lookup."""
    text = files(__package__).joinpath("mesh_trees.json").read_text(encoding="utf-8")
    trees: dict[str, list[str]] = json.loads(text)
    return trees
