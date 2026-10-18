"""A per-provider circuit breaker for the LLM fallback chain, in process memory.

After `failure_threshold` consecutive infrastructure failures the circuit opens and the
chain skips the provider without calling it for `open_seconds`; then one half-open probe
call goes through and closes the circuit on success or reopens it on failure. State
lives in this process only (docs/decisions.md names the shared-state path).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ctviz.schemas.llm import CircuitState

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_OPEN_SECONDS = 60.0


class CircuitBreaker:
    """Closed -> open after N consecutive failures -> half-open after a pause -> closed.

    `clock` returns monotonic seconds; tests inject a fake one.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        open_seconds: float = DEFAULT_OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self.consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        """The current state; open turns half-open once `open_seconds` have passed."""
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at < self._open_seconds:
            return CircuitState.OPEN
        return CircuitState.HALF_OPEN

    def allow(self) -> bool:
        """Whether a call may be made now; in half-open, only the first caller gets one."""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.OPEN or self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        """The provider answered (even if the answer was rejected): close the circuit."""
        self.consecutive_failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        """An infrastructure failure: count it; open at the threshold or after a probe."""
        self.consecutive_failures += 1
        if self._probe_in_flight or self.consecutive_failures >= self._failure_threshold:
            self._opened_at = self._clock()
        self._probe_in_flight = False
