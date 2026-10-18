"""Re-capture the studies in tests/fixtures/ with the current retrieval projection.

Fetches each NCT ID already present in the fixtures via `GET /studies/{nctId}` with
`STUDY_FIELDS` and writes the studies back in place. Top-level response keys
(`totalCount`, `nextPageToken`) and the original `_request` are kept, because the tests
use them to describe the query the page came from; `_recaptured` records this run.

Run: uv run python scripts/capture_fixtures.py
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from ctviz.retrieval.fields import STUDY_FIELDS

BASE_URL = "https://clinicaltrials.gov/api/v2"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIXTURE_NAMES = ("studies_pembrolizumab_page", "studies_multi_phase", "studies_multi_country")

# Observed: ~10 back-to-back requests succeed, then HTTP 429 with no Retry-After header.
MIN_REQUEST_INTERVAL_SECONDS = 1.3
MAX_RETRIES_ON_429 = 5

logger = logging.getLogger("capture_fixtures")


def fetch_study(client: httpx.Client, nct_id: str) -> dict[str, Any]:
    """Fetch one projected study, retrying 429 with exponential backoff."""
    params = {"fields": "|".join(STUDY_FIELDS)}
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS)
        response = client.get(f"{BASE_URL}/studies/{nct_id}", params=params)
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()
        backoff = MIN_REQUEST_INTERVAL_SECONDS * 2**attempt
        logger.warning("429 for %s; backing off %.1f s", nct_id, backoff)
        time.sleep(backoff)
    raise RuntimeError(f"{nct_id}: still rate-limited after {MAX_RETRIES_ON_429} retries")


def nct_id_of(study: dict[str, Any]) -> str:
    """The NCT ID of a raw study."""
    return study["protocolSection"]["identificationModule"]["nctId"]


def recapture(path: Path, client: httpx.Client, cache: dict[str, dict[str, Any]]) -> None:
    """Replace every study in one fixture file with a fresh projected copy."""
    payload = json.loads(path.read_text())
    studies = payload["response"]["studies"]
    for index, study in enumerate(studies):
        nct_id = nct_id_of(study)
        if nct_id not in cache:
            cache[nct_id] = fetch_study(client, nct_id)
        studies[index] = cache[nct_id]
    payload["_recaptured"] = {
        "url": f"{BASE_URL}/studies/{{nctId}}",
        "fields": "|".join(STUDY_FIELDS),
        "on": date.today().isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    logger.info("%s: %d studies recaptured", path.name, len(studies))


def main() -> None:
    """Recapture all fixture files, fetching each NCT ID once."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cache: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=30.0) as client:
        for name in FIXTURE_NAMES:
            recapture(FIXTURES_DIR / f"{name}.json", client, cache)
    logger.info("%d API calls", len(cache))


if __name__ == "__main__":
    main()
