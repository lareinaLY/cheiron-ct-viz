"""Structured warnings and assumptions: {code, severity, cohort, message} notices."""

from __future__ import annotations

from datetime import date

from ctviz.aggregate import build_network, group_by, group_cohorts
from ctviz.schemas.plan import Cohort, Dimension, Filters, QueryPlan, Relation
from ctviz.schemas.response import (
    DEFAULT_SEVERITY,
    CohortStats,
    Notice,
    NoticeCode,
    NoticeSeverity,
)
from ctviz.schemas.trial import Phase
from ctviz.spec import build_response
from tests.factories import make_record, mesh

AS_OF = date(2026, 10, 17)


def stats(label: str, count: int) -> CohortStats:
    return CohortStats(
        label=label,
        total_matched=count,
        records_retrieved=count,
        records_skipped=0,
        truncated=False,
        coverage=1.0,
        reconciled=True,
    )


def test_every_code_has_a_default_severity() -> None:
    assert set(DEFAULT_SEVERITY) == set(NoticeCode)
    assert {severity.value for severity in NoticeSeverity} == {"error", "warning", "info"}


def test_degraded_data_is_the_only_error_by_default() -> None:
    errors = [code for code, severity in DEFAULT_SEVERITY.items() if severity == "error"]
    assert errors == [NoticeCode.DEGRADED_DATA]


def test_notice_of_uses_the_default_severity_unless_given() -> None:
    plain = Notice.of(NoticeCode.UNKNOWN_BUCKET, "m")
    assert plain.severity == NoticeSeverity.INFO
    assert plain.cohort is None
    raised = Notice.of(NoticeCode.COHORTS_OVERLAP, "m", severity=NoticeSeverity.WARNING)
    assert raised.severity == NoticeSeverity.WARNING


def test_unknown_bucket_warning_has_its_code() -> None:
    records = [
        make_record("NCT1", phases=[Phase.PHASE2], phase_bucket="Phase 2"),
        make_record("NCT2"),
    ]
    result = group_by(records, Dimension.PHASE, as_of=AS_OF)
    (warning,) = result.warnings
    assert warning.code == NoticeCode.UNKNOWN_BUCKET
    assert "1 of 2" in warning.message


def test_dimension_notes_are_info_assumptions() -> None:
    result = group_by([make_record("NCT1")], Dimension.PHASE, as_of=AS_OF)
    assert {a.code for a in result.assumptions} == {NoticeCode.DIMENSION_NOTE}
    assert all(a.severity == NoticeSeverity.INFO for a in result.assumptions)


def test_comparison_warnings_name_their_cohort() -> None:
    plan = QueryPlan(
        cohorts=[
            Cohort(label="a", filters=Filters(drug="a")),
            Cohort(label="b", filters=Filters(drug="b")),
        ],
        dimension=Dimension.PHASE,
    )
    result = group_cohorts(
        [("a", [make_record("NCT1")]), ("b", [make_record("NCT2", phase_bucket="Phase 1")])],
        Dimension.PHASE,
        as_of=AS_OF,
    )
    response = build_response(
        plan,
        result,
        question="q",
        cohort_stats=[stats("a", 1), stats("b", 1)],
        search_semantics={},
        data_timestamp=None,
        timing_ms={},
    )
    (unknown,) = [w for w in response.meta.warnings if w.code == NoticeCode.UNKNOWN_BUCKET]
    assert unknown.cohort == "a"
    assert unknown.message.startswith("a: ")


def test_network_warnings_have_codes() -> None:
    records = [make_record("NCT1", intervention_mesh_terms=[mesh("pembrolizumab", "C582435")])]
    graph = build_network(records, Relation.DRUG_DRUG_COOCCURRENCE)
    codes = {w.code for w in graph.warnings}
    assert NoticeCode.NETWORK_SEMANTICS in codes
    assert NoticeCode.NETWORK_MISSING_VALUES in codes
