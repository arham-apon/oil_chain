"""Hand-written SSE parser and stream reader for ``GET /v1/stream`` (spec §7.3).

* Lines starting with ``:`` are comments (``: connected``, ``: keepalive``) — they only refresh ``last_seen``.
* ``event:`` + ``data:`` + blank line -> :class:`SSEEvent`.
* The read timeout is 45 s: keepalives arrive after 15 s of silence, so 15 s of quiet is normal.
* A 503 on connect means a ``stream_disconnect`` fault -> :class:`StreamFaultError`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from .exceptions import SimAPIError, StreamFaultError, StreamTimeoutError
from .schemas import SimError, SimErrorKind, SSEEvent

DEFAULT_READ_TIMEOUT_S = 45.0


class SSEParser:
    """Incremental line-based parser. Feed it one line at a time (without the trailing newline)."""

    def __init__(self) -> None:
        self._name: str | None = None
        self._data: list[str] = []
        self.last_comment: str | None = None

    def feed(self, line: str) -> SSEEvent | None:
        line = line.rstrip("\r")
        if line == "":  # blank line dispatches the buffered event
            if self._name is None and not self._data:
                return None
            raw = "\n".join(self._data)
            name = self._name or "message"
            self._name, self._data = None, []
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw
            return SSEEvent(name=name, data=data, raw=raw)
        if line.startswith(":"):
            self.last_comment = line[1:].strip()
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._name = value
        elif field == "data":
            self._data.append(value)
        # ``id`` / ``retry`` are irrelevant (no Last-Event-ID replay on this server)
        return None


async def parse_lines(
    lines: AsyncIterator[str], on_activity: Callable[[], None] | None = None
) -> AsyncIterator[SSEEvent]:
    """Turn an async iterator of text lines into SSE events. ``on_activity`` fires for every line."""
    parser = SSEParser()
    async for line in lines:
        if on_activity:
            on_activity()
        event = parser.feed(line)
        if event is not None:
            yield event


async def stream_events(
    http: httpx.AsyncClient,
    path: str = "/v1/stream",
    *,
    read_timeout: float = DEFAULT_READ_TIMEOUT_S,
    connect_timeout: float = 3.0,
    on_activity: Callable[[], None] | None = None,
) -> AsyncIterator[SSEEvent]:
    """Open the stream and yield events until it closes. Raises :class:`StreamFaultError` on a 503 connect."""
    from .sim_client import parse_sim_error  # local import: avoid a cycle

    timeout = httpx.Timeout(
        connect=connect_timeout, read=read_timeout, write=connect_timeout, pool=connect_timeout
    )
    try:
        async with http.stream("GET", path, timeout=timeout, headers={"Accept": "text/event-stream"}) as resp:
            if resp.status_code != 200:
                await resp.aread()
                err: SimError = parse_sim_error(resp)
                if resp.status_code == 503 or err.kind is SimErrorKind.FAULT:
                    raise StreamFaultError(f"stream unavailable: {err.code} {err.message}")
                raise SimAPIError(err, endpoint=path)
            if on_activity:
                on_activity()
            async for event in parse_lines(resp.aiter_lines(), on_activity):
                yield event
    except httpx.ReadTimeout as exc:
        raise StreamTimeoutError(f"no data from {path} for {read_timeout}s") from exc
