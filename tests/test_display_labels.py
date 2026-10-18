"""Enum buckets keep the registry code as key and get a human display label."""

from __future__ import annotations

from ctviz.aggregate import group_by
from ctviz.aggregate.registry import AGENCY_CLASS_LABELS, OVERALL_STATUS_LABELS
from ctviz.schemas.plan import Dimension
from ctviz.schemas.trial import AgencyClass, OverallStatus, Sponsor
from tests.factories import make_record


def test_every_status_and_sponsor_class_has_a_label() -> None:
    assert set(OVERALL_STATUS_LABELS) == set(OverallStatus)
    assert set(AGENCY_CLASS_LABELS) == set(AgencyClass)


def test_status_bucket_key_is_the_code_and_label_is_readable() -> None:
    records = [
        make_record("NCT1", overall_status=OverallStatus.ACTIVE_NOT_RECRUITING),
        make_record("NCT2", overall_status=OverallStatus.UNKNOWN),
    ]
    buckets = {b.key: b for b in group_by(records, Dimension.OVERALL_STATUS).buckets}
    assert buckets["ACTIVE_NOT_RECRUITING"].label == "Active, not recruiting"
    # The registry's UNKNOWN status is a real value, not the missing-value bucket.
    assert buckets["UNKNOWN"].label == "Unknown status (registry value)"
    assert buckets["UNKNOWN"].is_unknown is False
    assert buckets["ACTIVE_NOT_RECRUITING"].citations[0].value == "ACTIVE_NOT_RECRUITING"


def test_sponsor_class_other_says_what_it_means() -> None:
    sponsor = Sponsor(name="Mayo Clinic", agency_class=AgencyClass.OTHER)
    records = [make_record("NCT1", lead_sponsor=sponsor)]
    (bucket,) = group_by(records, Dimension.SPONSOR_CLASS).buckets
    assert bucket.key == "OTHER"
    assert bucket.label == "Other (academic, hospital, foundation)"
