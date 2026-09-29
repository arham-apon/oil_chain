"""WebSocket fan-out (spec §13.1): Redis stream ``fsp:events`` -> every connected browser.

The gateway reads the stream with plain ``XREAD`` (no consumer group) so each gateway replica sees every message.
``state.updated`` arrives up to 8x/s at full simulator speed; it is coalesced to at most ``STATE_MAX_HZ`` per second.
A ``health`` message is pushed every ``HEALTH_INTERVAL_S``. REST stays the source of truth: the UI treats every
message as a hint to re-fetch, and keeps polling if the socket is down.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket
from fsp_shared.bus import STREAM_KEY
from fsp_shared.logging import get_logger
from prometheus_client import Counter, Gauge

if TYPE_CHECKING:
    from .runtime import Runtime

log = get_logger("gateway.ws")
FORWARDED = {"state.updated", "decisions.updated", "alert", "sim.fault", "sim.reset", "event.changed",
             "allocation.changed"}
STATE_MAX_HZ = 2.0
HEALTH_INTERVAL_S = 5.0

WS_CLIENTS = Gauge("gateway_ws_clients", "Connected WebSocket clients")
WS_MESSAGES = Counter("gateway_ws_messages_total", "Messages pushed to WebSocket clients", ["type"])


class Hub:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.clients: set[WebSocket] = set()
        self.bus_connected = False
        self._last_state_push = 0.0
        self._pending_state: dict[str, Any] | None = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        WS_CLIENTS.set(len(self.clients))
        await self._send(ws, {"type": "hello", "payload": {"bus": self.bus_connected}})

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        WS_CLIENTS.set(len(self.clients))

    async def close_all(self) -> None:
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()

    async def _send(self, ws: WebSocket, msg: dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(ws.send_text(json.dumps(msg, default=str)), 2.0)
            return True
        except Exception:  # noqa: BLE001 - slow/closed client: drop it, never block the fan-out
            self.disconnect(ws)
            return False

    async def broadcast(self, typ: str, payload: dict[str, Any]) -> None:
        if not self.clients:
            return
        WS_MESSAGES.labels(typ).inc()
        msg = {"type": typ, "payload": payload}
        await asyncio.gather(*(self._send(ws, msg) for ws in list(self.clients)))

    async def _flush_state(self) -> None:
        if self._pending_state is not None and time.monotonic() - self._last_state_push >= 1.0 / STATE_MAX_HZ:
            payload, self._pending_state = self._pending_state, None
            self._last_state_push = time.monotonic()
            await self.broadcast("state.updated", payload)

    async def run_bus(self) -> None:
        backoff = 0.5
        last_id = "$"
        while True:
            try:
                resp = await self.rt.redis.xread({STREAM_KEY: last_id}, count=200, block=500)
                self.bus_connected = True
                backoff = 0.5
                for _, entries in resp or []:
                    for mid, fields in entries:
                        last_id = mid
                        typ = fields.get("type", "")
                        if typ not in FORWARDED:
                            continue
                        try:
                            payload = json.loads(fields.get("payload") or "{}")
                        except ValueError:
                            payload = {}
                        if typ == "state.updated":
                            self._pending_state = payload
                        else:
                            await self.broadcast(typ, payload)
                await self._flush_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Redis down: UI falls back to polling
                if self.bus_connected:
                    log.warning("ws_bus_unavailable", error=str(exc)[:150])
                self.bus_connected = False
                last_id = "$"
                await asyncio.sleep(random.uniform(backoff / 2, backoff))
                backoff = min(10.0, backoff * 2)

    async def run_health(self) -> None:
        from .routers.health import components  # local import: routers import the runtime module

        while True:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            if not self.clients:
                continue
            try:
                await self.broadcast("health", await components(self.rt))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("ws_health_failed", error=str(exc)[:150])
