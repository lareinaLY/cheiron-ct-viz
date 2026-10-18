"""Cohort labels state what a free-text filter really matched."""

from __future__ import annotations

from ctviz.schemas.plan import Filters
from ctviz.schemas.request import cohort_label


def test_sponsor_label_says_lead_or_collaborator_and_name_match() -> None:
    assert cohort_label(Filters(drug="pembrolizumab", sponsor=" Merck ")) == (
        "pembrolizumab, sponsored by Merck (lead sponsor or collaborator; name contains 'Merck')"
    )
