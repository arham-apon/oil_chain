"""Exception types raised by the simulator client and SSE reader."""

from __future__ import annotations

from typing import Any

from .schemas import SimError


class SimClientError(Exception):
    """Base class for everything the simulator client raises."""


class SimAPIError(SimClientError):
    """The simulator answered with a non-success status (after retries where applicable)."""

    def __init__(self, error: SimError, *, endpoint: str = "") -> None:
        super().__init__(f"{endpoint} -> HTTP {error.status} {error.code}: {error.message}")
        self.error = error
        self.endpoint = endpoint

    @property
    def status(self) -> int:
        return self.error.status

    @property
    def code(self) -> str:
        return self.error.code


class SimTransportError(SimClientError):
    """Timeouts / connection errors that survived every retry."""

    def __init__(self, endpoint: str, cause: BaseException) -> None:
        super().__init__(f"{endpoint}: {type(cause).__name__}: {cause}")
        self.endpoint = endpoint
        self.cause = cause


class SimPayloadError(SimClientError):
    """2xx response whose body failed schema validation (malformed-data guard, spec §9.3)."""

    def __init__(self, endpoint: str, errors: Any) -> None:
        super().__init__(f"{endpoint}: invalid payload: {errors}")
        self.endpoint = endpoint
        self.errors = errors


class StreamError(SimClientError):
    """Base class for SSE problems."""


class StreamFaultError(StreamError):
    """503 on connect: a ``stream_disconnect`` fault is active."""


class StreamTimeoutError(StreamError):
    """No bytes (not even keepalives) within the read timeout."""


class BreakerOpenError(Exception):
    """Raised instead of calling a dependency whose circuit breaker is OPEN."""

    def __init__(self, dependency: str) -> None:
        super().__init__(f"circuit breaker for '{dependency}' is open")
        self.dependency = dependency
