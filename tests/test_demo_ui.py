"""Static checks of the served demo UI: how notice severities map to page containers.

The page has no JS test runner, so these read the served HTML. They pin the contract the
QA pass found broken (docs/ui_test_report.md, finding 1): every severity has its own
container, errors and warnings above the chart, info below it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ctviz.api.app import create_app
from ctviz.config import Settings
from ctviz.schemas.response import NoticeSeverity


@pytest.fixture(scope="module")
def html() -> Iterator[str]:
    settings = Settings(_env_file=None, enum_drift_check=False)  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        yield client.get("/").text


def notice_containers(html: str) -> dict[str, str]:
    match = re.search(r"const NOTICE_CONTAINERS = (\{[^}]*\});", html)
    assert match, "the page declares NOTICE_CONTAINERS as a JSON-style object literal"
    return json.loads(match.group(1))


def position_of_id(html: str, element_id: str) -> int:
    match = re.search(rf'<[a-z0-9]+ [^>]*id="{element_id}"', html)
    assert match, f"no element with id={element_id!r} in the markup"
    return match.start()


def test_each_severity_has_its_own_container(html: str) -> None:
    containers = notice_containers(html)
    assert set(containers) == {severity.value for severity in NoticeSeverity}
    assert len(set(containers.values())) == len(containers)


def test_errors_and_warnings_sit_above_the_chart_and_info_below(html: str) -> None:
    containers = notice_containers(html)
    chart = position_of_id(html, "chart")
    assert position_of_id(html, containers["error"]) < chart
    assert position_of_id(html, containers["warning"]) < chart
    assert position_of_id(html, containers["error"]) < position_of_id(html, containers["warning"])
    assert position_of_id(html, containers["info"]) > chart


def test_notice_containers_are_shown_when_filled(html: str) -> None:
    # The QA bug: warnings were appended to a container that stayed hidden.
    assert "show(NOTICE_CONTAINERS[severity], notices.length > 0)" in html
