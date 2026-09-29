"""Circuit breaker (CLOSED -> OPEN -> HALF_OPEN) used for the ``jev``, ``gemini`` and ``forecast`` dependencies."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import TypeVar

from .exceptions import BreakerOpenError
from .metrics import BREAKER_STATE, FALLBACK_ACTIVATIONS

T = TypeVar("T")


class BreakerState(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures (a call exceeding ``timeout`` counts as one).

    While OPEN every call is rejected with :class:`BreakerOpenError` for ``open_seconds``. After that a
    single HALF_OPEN probe is allowed: success closes the breaker, failure re-opens it.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
        timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.timeout = timeout
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        BREAKER_STATE.labels(name).set(int(self._state))

    # --- state ---------------------------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self.open_seconds:
            self._set_state(BreakerState.HALF_OPEN)
            self._probe_in_flight = False
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def _set_state(self, state: BreakerState) -> None:
        self._state = state
        BREAKER_STATE.labels(self.name).set(int(state))

    # --- admission -----------------------------------------------------------------------
    def allow(self) -> bool:
        """True if a call may proceed now. In HALF_OPEN only one probe is admitted."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._probe_in_flight = False
        if self._state is not BreakerState.CLOSED:
            self._set_state(BreakerState.CLOSED)

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self._state is BreakerState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._state is BreakerState.CLOSED and self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._opened_at = self._clock()
        self._set_state(BreakerState.OPEN)

    def record_fallback(self, reason: str) -> None:
        FALLBACK_ACTIVATIONS.labels(self.name, reason).inc()

    # --- convenience ----------------------------------------------------------------------
    async def call(self, fn: Callable[[], Awaitable[T]], *, timeout: float | None = None) -> T:
        """Run ``fn`` under the breaker. Raises :class:`BreakerOpenError` if not admitted;
        other exceptions (including ``asyncio.TimeoutError``) are recorded as failures and re-raised."""
        if not self.allow():
            self.record_fallback("breaker_open")
            raise BreakerOpenError(self.name)
        limit = timeout if timeout is not None else self.timeout
        try:
            result = await (asyncio.wait_for(fn(), limit) if limit else fn())
        except BaseException:
            self.record_failure()
            raise
        self.record_success()
        return result
