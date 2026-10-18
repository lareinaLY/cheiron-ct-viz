"""Download the MeSH tree-number file from NLM and write a compact JSON lookup.

Input: `mtrees<year>.bin`, one "Preferred Name;TreeNumber" line per tree position (a
descriptor with several positions has several lines). Output:
`src/ctviz/data/mesh_trees.json`, a {preferred_name: [tree_numbers]} object used to keep
only Chemicals and Drugs (branch D) descriptors as drug network nodes.

When the full tree numbers exceed `MAX_OUTPUT_BYTES`, only the distinct first letters
(the top-level branch) are kept, which is all the network filter reads.

Source and license: docs/api_findings.md §18.

Run: uv run python scripts/fetch_mesh_trees.py [--year 2025]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import httpx

TREES_URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/{year}/meshtrees/mtrees{year}.bin"
DEFAULT_YEAR = 2025
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "src" / "ctviz" / "data" / "mesh_trees.json"
MAX_OUTPUT_BYTES = 3_000_000

logger = logging.getLogger("fetch_mesh_trees")


def download(year: int) -> str:
    """Fetch the tree file for `year`; raises on any HTTP error (NLM redirects a missing
    year to an HTML error page, so a non-text body is rejected too)."""
    url = TREES_URL.format(year=year)
    response = httpx.get(url, timeout=60.0, follow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise RuntimeError(f"{url} answered {response.status_code}; is {year} published?")
    logger.info("Downloaded %s (%d bytes)", url, len(response.content))
    return response.text


def parse_trees(text: str) -> dict[str, list[str]]:
    """Map each preferred name to its tree numbers, in file order.

    Lines carry a leading space in the published file, so names are stripped.
    """
    trees: dict[str, list[str]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        name, separator, tree_number = line.strip().rpartition(";")
        if not separator or not name or not tree_number:
            raise ValueError(f"line {line_number} is not 'Name;TreeNumber': {line!r}")
        trees.setdefault(name, []).append(tree_number)
    return trees


def branches_only(trees: dict[str, list[str]]) -> dict[str, list[str]]:
    """Reduce each term's tree numbers to its distinct top-level branch letters."""
    return {name: sorted({number[0] for number in numbers}) for name, numbers in trees.items()}


def serialize(trees: dict[str, list[str]]) -> str:
    """Compact, key-sorted JSON so regenerating with the same input gives the same file."""
    return json.dumps(trees, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> None:
    """Download, parse and write the lookup, shrinking it if it is over the size budget."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    args = parser.parse_args()

    trees = parse_trees(download(args.year))
    payload = serialize(trees)
    if len(payload.encode()) > MAX_OUTPUT_BYTES:
        logger.info("Full tree numbers are %d bytes; keeping branch letters only", len(payload))
        payload = serialize(branches_only(trees))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(payload, encoding="utf-8")
    logger.info("Wrote %d terms, %d bytes to %s", len(trees), len(payload.encode()), OUTPUT_PATH)


if __name__ == "__main__":
    main()
