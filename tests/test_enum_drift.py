"""Startup enum drift check: GET /studies/enums compared with our enums, never blocking."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from enum import StrEnum
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.retrieval.cache import CachedResponse, NoCache
from ctviz.retrieval.client import CtGovClient
from ctviz.retrieval.enums import KNOWN_ENUMS, check_enum_drift, enum_drift
from ctviz.schemas.retrieval import EnumDrift
from ctviz.schemas.trial import AgencyClass, InterventionType, OverallStatus, Phase, StudyType

BASE_URL = "https://ctgov.test/api/v2"


def enum_entry(type_name: str, values: list[str]) -> dict[str, Any]:
    """One /studies/enums entry, shaped like the live API (docs/api_findings.md §1)."""
    return {
        "type": type_name,
        "pieces": [type_name],
        "values": [{"value": value, "legacyValue": value.title()} for value in values],
    }


def values_of(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


def current_payload() -> list[dict[str, Any]]:
    """What the API serves today: exactly our enums, plus types we do not use."""
    return [
        enum_entry("Phase", values_of(Phase)),
        enum_entry("Status", values_of(OverallStatus)),
        enum_entry("StudyType", values_of(StudyType)),
        enum_entry("AgencyClass", values_of(AgencyClass)),
        enum_entry("InterventionType", values_of(InterventionType)),
        enum_entry("DateType", ["ACTUAL", "ESTIMATED"]),
    ]


def drifted_payload() -> list[dict[str, Any]]:
    payload = current_payload()
    payload[0] = enum_entry("Phase", [*values_of(Phase), "PHASE5"])
    payload[1] = enum_entry("Status", [*values_of(OverallStatus), "PAUSED_FOREVER", "ON_HOLD"])
    payload[4] = enum_entry("InterventionType", [*values_of(InterventionType), "GENE_EDITING"])
    return payload


class TestEnumDrift:
    def test_compares_the_five_enums(self) -> None:
        assert set(KNOWN_ENUMS) == {
            "Phase",
            "Status",
            "StudyType",
            "AgencyClass",
            "InterventionType",
        }

    def test_no_drift(self) -> None:
        drift = enum_drift(current_payload())
        assert drift == EnumDrift(new_values={}, missing_types=[])
        assert drift.has_drift is False

    def test_new_values_are_listed_per_type(self) -> None:
        drift = enum_drift(drifted_payload())
        assert drift.new_values == {
            "Phase": ["PHASE5"],
            "Status": ["ON_HOLD", "PAUSED_FOREVER"],
            "InterventionType": ["GENE_EDITING"],
        }
        assert drift.has_drift is True

    def test_missing_enum_type_is_drift(self) -> None:
        payload = [entry for entry in current_payload() if entry["type"] != "AgencyClass"]
        drift = enum_drift(payload)
        assert drift.missing_types == ["AgencyClass"]
        assert drift.has_drift is True

    def test_values_we_know_but_the_api_dropped_are_not_drift(self) -> None:
        payload = current_payload()
        payload[0] = enum_entry("Phase", ["PHASE1", "PHASE2"])
        assert enum_drift(payload).has_drift is False

    @pytest.mark.parametrize(
        "payload",
        [{"Phase": []}, [{"type": "Phase"}], [{"type": "Phase", "values": [{"v": "X"}]}], ["x"]],
    )
    def test_malformed_payload_raises_value_error(self, payload: Any) -> None:
        with pytest.raises(ValueError, match="/studies/enums"):
            enum_drift(payload)


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ctgov_base_url": BASE_URL,
        "min_request_interval_seconds": 0.0,
        "backoff_base_seconds": 0.0,
        "max_retries": 1,
        "cache_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
async def client() -> AsyncIterator[CtGovClient]:
    async with CtGovClient(settings(), cache=NoCache()) as instance:
        yield instance


def warnings_from(caplog: pytest.LogCaptureFixture) -> list[str]:
    """WARNING lines of the drift check itself (the client logs its own retries)."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "ctviz.retrieval.enums"
    ]


class TestCheckEnumDrift:
    async def test_drift_is_one_warning_line(
        self, client: CtGovClient, mock_api: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_api.get("/studies/enums").mock(
            return_value=httpx.Response(200, json=drifted_payload())
        )
        with caplog.at_level(logging.INFO, logger="ctviz.retrieval.enums"):
            drift = await check_enum_drift(client)
        assert drift is not None
        assert drift.has_drift is True
        (line,) = warnings_from(caplog)
        for value in ("PHASE5", "PAUSED_FOREVER", "ON_HOLD", "GENE_EDITING"):
            assert value in line

    async def test_no_drift_logs_no_warning(
        self, client: CtGovClient, mock_api: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_api.get("/studies/enums").mock(
            return_value=httpx.Response(200, json=current_payload())
        )
        with caplog.at_level(logging.INFO, logger="ctviz.retrieval.enums"):
            drift = await check_enum_drift(client)
        assert drift is not None
        assert drift.has_drift is False
        assert warnings_from(caplog) == []

    async def test_upstream_failure_is_logged_not_raised(
        self, client: CtGovClient, mock_api: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_api.get("/studies/enums").mock(return_value=httpx.Response(503))
        with caplog.at_level(logging.INFO, logger="ctviz.retrieval.enums"):
            assert await check_enum_drift(client) is None
        (line,) = warnings_from(caplog)
        assert "skipped" in line

    async def test_malformed_payload_is_logged_not_raised(
        self, client: CtGovClient, mock_api: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_api.get("/studies/enums").mock(return_value=httpx.Response(200, json={"x": 1}))
        with caplog.at_level(logging.INFO, logger="ctviz.retrieval.enums"):
            assert await check_enum_drift(client) is None
        assert len(warnings_from(caplog)) == 1


class ListCache:
    """In-memory cache that accepts any JSON payload, as the enums body is a list."""

    def __init__(self) -> None:
        self.entries: dict[str, CachedResponse] = {}

    def get(self, key: str) -> CachedResponse | None:
        return self.entries.get(key)

    def set(self, key: str, payload: Any) -> None:
        self.entries[key] = CachedResponse(payload=payload, stored_at=datetime(2026, 10, 18))


async def test_enums_are_cached(mock_api: respx.MockRouter) -> None:
    route = mock_api.get("/studies/enums").mock(
        return_value=httpx.Response(200, json=current_payload())
    )
    async with CtGovClient(settings(), cache=ListCache()) as cached_client:
        first = await cached_client.fetch_enums()
        second = await cached_client.fetch_enums()
    assert first == second == current_payload()
    assert route.call_count == 1


def test_startup_is_not_blocked_by_a_failing_enums_endpoint(mock_api: respx.MockRouter) -> None:
    mock_api.get("/studies/enums").mock(return_value=httpx.Response(500))
    with TestClient(create_app(settings(enum_drift_check=True))) as api:
        assert api.get("/health").status_code == 200
        assert api.app.state.enum_drift_task is not None  # type: ignore[attr-defined]


def test_startup_check_can_be_disabled(mock_api: respx.MockRouter) -> None:
    route = mock_api.get("/studies/enums").mock(return_value=httpx.Response(500))
    with TestClient(create_app(settings(enum_drift_check=False))) as api:
        assert api.get("/health").status_code == 200
        assert api.app.state.enum_drift_task is None  # type: ignore[attr-defined]
    assert route.call_count == 0
