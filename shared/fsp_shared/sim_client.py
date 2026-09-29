"""Defensive async client for the BUP Fuel Supply Simulator (spec §7.2).

* One shared ``httpx.AsyncClient`` per service; typed methods for every ``/v1/*`` endpoint.
* Every call returns :class:`SimResponse` (``data, stale, status, latency_ms, tick_hint``).
* GET retry: 503 / timeouts / connection errors, exponential backoff with **full jitter**
  (base 100 ms, cap 2 s, max 4 attempts). 4xx is never retried.
* POST allocation retry: same retryable set (max 5 attempts) and the **identical body and
  idempotency key on every attempt** — the body is serialised once, before the loop.
* The simulator emits three error shapes (C16); :func:`parse_sim_error` understands all of them.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from . import sse
from .exceptions import SimAPIError, SimPayloadError, SimTransportError
from .metrics import SIM_CLIENT_LATENCY, SIM_CLIENT_REQUESTS, SIM_FAULTS_DETECTED
from .schemas import (
    Allocation,
    AllocationRequest,
    AuditRow,
    DemandRow,
    Depot,
    Health,
    Instance,
    Metrics,
    Region,
    Route,
    SimError,
    SimErrorKind,
    SimEvent,
    SimResponse,
    SimulatorState,
    SSEEvent,
    Station,
    SupplyArrival,
)

M = TypeVar("M")

RETRYABLE_STATUSES = frozenset({502, 503, 504})
STALE_HEADER = "X-Simulator-Stale"
SLOW_REQUEST_S = 1.0

GET_MAX_ATTEMPTS = 4
POST_MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 0.1
BACKOFF_CAP_S = 2.0


def backoff_delay(attempt: int, base: float = BACKOFF_BASE_S, cap: float = BACKOFF_CAP_S) -> float:
    """Full-jitter exponential backoff: ``uniform(0, min(cap, base * 2**attempt))`` (attempt is 0-based)."""
    return random.uniform(0.0, min(cap, base * (2**attempt)))


def parse_sim_error(resp: httpx.Response) -> SimError:
    """Normalise the simulator's three error bodies (C16) into a :class:`SimError`.

    * ``{"error": {"code": "FAULT_INJECTED", ...}}``  — injected ``unavailable`` / ``error_rate`` faults
    * ``{"detail": {"code": ..., "message": ...}}``    — allocation errors and ``stream_disconnect``
    * ``{"detail": [ ... ]}``                          — Pydantic 422 validation errors
    """
    status = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        return SimError(f"HTTP_{status}", (resp.text or "")[:200], SimErrorKind.UNKNOWN, status)

    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "UNKNOWN")
            kind = SimErrorKind.FAULT if code == "FAULT_INJECTED" else SimErrorKind.UNKNOWN
            return SimError(code, str(err.get("message") or ""), kind, status)
        detail = body.get("detail")
        if isinstance(detail, dict):
            code = str(detail.get("code") or "UNKNOWN")
            kind = SimErrorKind.FAULT if code == "FAULT_INJECTED" else SimErrorKind.DOMAIN
            return SimError(code, str(detail.get("message") or ""), kind, status)
        if isinstance(detail, list):
            parts = []
            for item in detail[:3]:
                if isinstance(item, dict):
                    loc = ".".join(str(p) for p in item.get("loc", []))
                    parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
            return SimError("VALIDATION_ERROR", "; ".join(parts), SimErrorKind.VALIDATION, status)
        if isinstance(detail, str):
            return SimError(f"HTTP_{status}", detail, SimErrorKind.UNKNOWN, status)
    return SimError(f"HTTP_{status}", str(body)[:200], SimErrorKind.UNKNOWN, status)


class SimClient:
    """Async simulator client. Create one per service and ``await client.aclose()`` on shutdown."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 3.0,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        get_max_attempts: int = GET_MAX_ATTEMPTS,
        post_max_attempts: int = POST_MAX_ATTEMPTS,
    ) -> None:
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._sleep = sleep
        self._get_max_attempts = get_max_attempts
        self._post_max_attempts = post_max_attempts
        # observable state ------------------------------------------------------------
        self.last_tick: int | None = None
        self.last_success_at: float | None = None
        self.last_stale: bool = False
        self.consecutive_v1_failures: int = 0
        self.state: SimulatorState = SimulatorState.UP
        self.last_stream_activity: float | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> SimClient:
        return cls(settings.SIMULATOR_URL, settings.SIM_HTTP_TIMEOUT_S)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------------------
    # low-level request loop
    # ------------------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        endpoint: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        max_attempts: int,
        track_state: bool = True,
    ) -> tuple[httpx.Response, float, bool]:
        """Send the request, retrying transient failures. Returns ``(response, latency_ms, stale)`` for 2xx;
        raises :class:`SimAPIError` / :class:`SimTransportError` otherwise. ``json_body`` is reused verbatim."""
        last_exc: BaseException | None = None
        last_error: SimError | None = None
        for attempt in range(max_attempts):
            started = time.perf_counter()
            try:
                resp = await self._http.request(method, path, json=json_body, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc, last_error = exc, None
                SIM_CLIENT_REQUESTS.labels(
                    endpoint, method, "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                ).inc()
                SIM_CLIENT_LATENCY.labels(endpoint).observe(time.perf_counter() - started)
                if track_state:
                    self._note_failure()
                if attempt + 1 < max_attempts:
                    await self._sleep(backoff_delay(attempt))
                    continue
                raise SimTransportError(endpoint, exc) from exc

            elapsed = time.perf_counter() - started
            latency_ms = elapsed * 1000.0
            stale = resp.headers.get(STALE_HEADER, "").lower() == "true"
            SIM_CLIENT_REQUESTS.labels(endpoint, method, str(resp.status_code)).inc()
            SIM_CLIENT_LATENCY.labels(endpoint).observe(elapsed)
            if stale:
                SIM_FAULTS_DETECTED.labels("stale").inc()
            if elapsed > SLOW_REQUEST_S:
                SIM_FAULTS_DETECTED.labels("latency").inc()

            if 200 <= resp.status_code < 300:
                if track_state:
                    self._note_success(stale)
                return resp, latency_ms, stale

            error = parse_sim_error(resp)
            last_error = error
            if error.kind is SimErrorKind.FAULT:
                SIM_FAULTS_DETECTED.labels("fault").inc()
            if resp.status_code in RETRYABLE_STATUSES:
                if track_state:
                    self._note_failure()
                if attempt + 1 < max_attempts:
                    await self._sleep(backoff_delay(attempt))
                    continue
            # 4xx (and exhausted 5xx): a domain answer, not a transport problem -> surface it
            elif track_state:
                self._note_success(False)  # the simulator answered coherently
            raise SimAPIError(error, endpoint=endpoint)
        # unreachable, kept for type-checkers
        if last_error is not None:
            raise SimAPIError(last_error, endpoint=endpoint)
        raise SimTransportError(endpoint, last_exc or RuntimeError("request failed"))

    def _note_success(self, stale: bool) -> None:
        self.consecutive_v1_failures = 0
        self.last_success_at = time.time()
        self.last_stale = stale
        if self.state is not SimulatorState.DOWN:
            self.state = SimulatorState.UP

    def _note_failure(self) -> None:
        self.consecutive_v1_failures += 1

    def _parse(self, model: Any, resp: httpx.Response, endpoint: str) -> Any:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SimPayloadError(endpoint, f"body is not JSON: {exc}") from exc
        try:
            return TypeAdapter(model).validate_python(payload)
        except ValidationError as exc:
            raise SimPayloadError(endpoint, exc.errors(include_url=False, include_input=False)) from exc

    async def _get(
        self, path: str, model: Any, *, endpoint: str | None = None, params: dict[str, Any] | None = None
    ) -> SimResponse[Any]:
        label = endpoint or path
        resp, latency_ms, stale = await self._request(
            "GET", path, label, params=params, max_attempts=self._get_max_attempts
        )
        data = self._parse(model, resp, label)
        if isinstance(data, Instance):
            self.last_tick = data.tick
        return SimResponse(
            data=data, stale=stale, status=resp.status_code, latency_ms=latency_ms, tick_hint=self.last_tick
        )

    # ------------------------------------------------------------------------------
    # /v1 reads
    # ------------------------------------------------------------------------------
    async def health(self) -> SimResponse[Health]:
        """Liveness probe. Bypasses faults, so it is never retried and never touches the failure counters."""
        resp, latency_ms, stale = await self._request(
            "GET", "/v1/health", "/v1/health", max_attempts=1, track_state=False
        )
        return SimResponse(
            self._parse(Health, resp, "/v1/health"), stale, resp.status_code, latency_ms, self.last_tick
        )

    async def instance(self) -> SimResponse[Instance]:
        return await self._get("/v1/instance", Instance)

    async def regions(self) -> SimResponse[list[Region]]:
        return await self._get("/v1/regions", list[Region])

    async def depots(self) -> SimResponse[list[Depot]]:
        return await self._get("/v1/depots", list[Depot])

    async def depot(self, depot_id: str) -> SimResponse[Depot]:
        return await self._get(f"/v1/depots/{depot_id}", Depot, endpoint="/v1/depots/{id}")

    async def stations(self) -> SimResponse[list[Station]]:
        return await self._get("/v1/stations", list[Station])

    async def station(self, station_id: str) -> SimResponse[Station]:
        return await self._get(f"/v1/stations/{station_id}", Station, endpoint="/v1/stations/{id}")

    async def routes(self) -> SimResponse[list[Route]]:
        return await self._get("/v1/routes", list[Route])

    async def supply_arrivals(self) -> SimResponse[list[SupplyArrival]]:
        return await self._get("/v1/supply-arrivals", list[SupplyArrival])

    async def events(self) -> SimResponse[list[SimEvent]]:
        return await self._get("/v1/events", list[SimEvent])

    async def allocations(self) -> SimResponse[list[Allocation]]:
        return await self._get("/v1/allocations", list[Allocation])

    async def demand_history(
        self, station_id: str | None = None, limit: int = 200
    ) -> SimResponse[list[DemandRow]]:
        """Always passes an explicit ``limit`` (clamped to [1, 2000]) — the table grows forever."""
        params: dict[str, Any] = {"limit": max(1, min(2000, int(limit)))}
        if station_id:
            params["station_id"] = station_id
        return await self._get("/v1/demand-history", list[DemandRow], params=params)

    async def metrics(self) -> SimResponse[Metrics]:
        return await self._get("/v1/metrics", Metrics)

    # ------------------------------------------------------------------------------
    # /v1 writes (the only domain writes)
    # ------------------------------------------------------------------------------
    async def post_allocation(self, request: AllocationRequest) -> SimResponse[Allocation]:
        """Create an allocation. HTTP 200 (replay) and 201 (created) are both success (C4).

        The body is serialised once so every retry carries the same bytes and the same idempotency key.
        """
        body = request.model_dump(mode="json")
        resp, latency_ms, stale = await self._request(
            "POST", "/v1/allocations", "/v1/allocations", json_body=body, max_attempts=self._post_max_attempts
        )
        return SimResponse(
            self._parse(Allocation, resp, "/v1/allocations"),
            stale,
            resp.status_code,
            latency_ms,
            self.last_tick,
        )

    async def cancel_allocation(self, allocation_id: int) -> SimResponse[Allocation]:
        endpoint = "/v1/allocations/{id}/cancel"
        resp, latency_ms, stale = await self._request(
            "POST", f"/v1/allocations/{allocation_id}/cancel", endpoint, max_attempts=self._get_max_attempts
        )
        return SimResponse(
            self._parse(Allocation, resp, endpoint), stale, resp.status_code, latency_ms, self.last_tick
        )

    # ------------------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------------------
    def _mark_stream_activity(self) -> None:
        self.last_stream_activity = time.time()

    def stream(
        self, read_timeout: float = sse.DEFAULT_READ_TIMEOUT_S, on_connect: Callable[[], None] | None = None
    ) -> AsyncIterator[SSEEvent]:
        """Async iterator over ``/v1/stream`` events. ``on_connect`` fires once the server answers 200
        (before any event: an idle paused simulator only sends comments). Raises ``StreamFaultError`` /
        ``StreamTimeoutError``."""
        return sse.stream_events(
            self._http,
            "/v1/stream",
            read_timeout=read_timeout,
            on_activity=self._mark_stream_activity,
            on_connect=on_connect,
        )

    # ------------------------------------------------------------------------------
    # state assessment
    # ------------------------------------------------------------------------------
    async def probe_state(self) -> SimulatorState:
        """UP / DEGRADED / DOWN. ``/v1/health`` bypasses faults, so: health unreachable -> DOWN;
        health fine but recent ``/v1/*`` calls failing -> DEGRADED (a fault is active); otherwise UP."""
        try:
            await self.health()
        except SimClientErrorTypes:
            self.state = SimulatorState.DOWN
            return self.state
        self.state = SimulatorState.DEGRADED if self.consecutive_v1_failures > 0 else SimulatorState.UP
        return self.state

    # ------------------------------------------------------------------------------
    # admin (tests / chaos / demo only — never on the production decision path)
    # ------------------------------------------------------------------------------
    async def _admin(
        self, method: str, path: str, json_body: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        resp, _, _ = await self._request(
            method, path, path, json_body=json_body, params=params, max_attempts=2, track_state=False
        )
        return resp.json()

    async def admin_run(self) -> Any:
        return await self._admin("POST", "/admin/run")

    async def admin_pause(self) -> Any:
        return await self._admin("POST", "/admin/pause")

    async def admin_toggle(self) -> Any:
        return await self._admin("POST", "/admin/toggle")

    async def admin_step(self) -> Any:
        return await self._admin("POST", "/admin/step")

    async def admin_reset(self) -> Any:
        return await self._admin("POST", "/admin/reset")

    async def admin_create_event(
        self, type: str, start_tick: int, duration_ticks: int, parameters: dict[str, Any] | None = None
    ) -> Any:
        body = {
            "type": type,
            "start_tick": start_tick,
            "duration_ticks": duration_ticks,
            "parameters": parameters or {},
        }
        return await self._admin("POST", "/admin/events", body)

    async def admin_create_fault(
        self, type: str, duration_seconds: int, parameters: dict[str, Any] | None = None
    ) -> Any:
        body = {"type": type, "duration_seconds": duration_seconds, "parameters": parameters or {}}
        return await self._admin("POST", "/admin/faults", body)

    async def admin_clear_faults(self) -> Any:
        return await self._admin("POST", "/admin/faults/clear")

    async def admin_faults(self) -> Any:
        return await self._admin("GET", "/admin/faults")

    async def admin_events(self) -> Any:
        return await self._admin("GET", "/admin/events")

    async def admin_audit(self, limit: int = 200) -> list[AuditRow]:
        data = await self._admin("GET", "/admin/audit", params={"limit": max(1, min(1000, limit))})
        return TypeAdapter(list[AuditRow]).validate_python(data)


# exceptions that mean "the simulator could not be reached / answered badly" for probe_state()
SimClientErrorTypes = (SimAPIError, SimTransportError, SimPayloadError)

__all__ = ["BaseModel", "SimClient", "backoff_delay", "parse_sim_error"]
