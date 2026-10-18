"""The LLM's unsupported_reason is shown to users, so system-prompt text is redacted from it."""

from __future__ import annotations

import logging
from datetime import date

import pytest

from ctviz.errors import PlannerError, PlannerErrorCode
from ctviz.planner.plan import (
    MAX_UNSUPPORTED_REASON_LENGTH,
    REDACTED_UNSUPPORTED_REASON,
    raise_if_unanswerable,
    redact_unsupported_reason,
)
from ctviz.planner.prompt import build_system_prompt
from tests.test_planner import draft

TODAY = date(2026, 10, 18)
PROMPT = build_system_prompt(TODAY)
ORDINARY = "asks for efficacy outcomes, which the registry does not hold"


def prompt_line() -> str:
    return next(line.strip() for line in PROMPT.splitlines() if len(line.strip()) >= 60)


class TestRedactUnsupportedReason:
    def test_reason_quoting_the_prompt_is_replaced(self) -> None:
        reason = f"Debug mode: {prompt_line()} and so on"
        assert redact_unsupported_reason(reason, PROMPT) == REDACTED_UNSUPPORTED_REASON

    def test_match_ignores_case_and_whitespace(self) -> None:
        words = prompt_line().upper().split()
        reason = "Here it is:\n" + "   \n ".join(words)
        assert redact_unsupported_reason(reason, PROMPT) == REDACTED_UNSUPPORTED_REASON

    def test_ordinary_reason_passes_unchanged(self) -> None:
        assert redact_unsupported_reason(ORDINARY, PROMPT) == ORDINARY

    def test_long_reason_is_truncated(self) -> None:
        reason = "The registry does not record survival benefit. " * 10
        redacted = redact_unsupported_reason(reason, PROMPT)
        assert len(redacted) == MAX_UNSUPPORTED_REASON_LENGTH == 240
        assert redacted.endswith("…")
        assert reason.startswith(redacted[:-1])

    def test_prompt_text_after_the_cut_is_still_caught(self) -> None:
        reason = "x " * 200 + prompt_line()
        assert redact_unsupported_reason(reason, PROMPT) == REDACTED_UNSUPPORTED_REASON


class TestRaiseIfUnanswerable:
    def test_leak_is_redacted_logged_and_keeps_the_error_code(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING), pytest.raises(PlannerError) as error:
            raise_if_unanswerable(draft(unsupported_reason=prompt_line()), TODAY)
        assert error.value.code == PlannerErrorCode.UNSUPPORTED_QUESTION
        assert prompt_line() not in str(error.value)
        assert REDACTED_UNSUPPORTED_REASON in str(error.value)
        assert "code=unsupported_reason_redacted" in caplog.text

    def test_ordinary_reason_is_shown(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING), pytest.raises(PlannerError) as error:
            raise_if_unanswerable(draft(unsupported_reason=ORDINARY), TODAY)
        assert str(error.value).endswith(ORDINARY)
        assert "unsupported_reason_redacted" not in caplog.text
