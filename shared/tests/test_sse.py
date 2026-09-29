import httpx
import pytest
import respx

from fsp_shared.exceptions import StreamFaultError, StreamTimeoutError
from fsp_shared.sim_client import SimClient
from fsp_shared.sse import SSEParser, parse_lines

BASE = "http://sim.test"


def feed_all(text: str):
    p = SSEParser()
    return [e for line in text.split("\n") if (e := p.feed(line)) is not None]


def test_comments_are_ignored_but_recorded():
    p = SSEParser()
    assert p.feed(": connected") is None and p.last_comment == "connected"
    assert p.feed("") is None  # blank line after a comment dispatches nothing
    assert p.feed(": keepalive") is None and p.last_comment == "keepalive"


def test_single_event():
    [e] = feed_all('event: simulation.tick\ndata: {"tick": 3, "sim_time": "2026-01-01T00:45:00"}\n\n')
    assert e.name == "simulation.tick" and e.data == {"tick": 3, "sim_time": "2026-01-01T00:45:00"}


def test_multiple_events_in_one_chunk_with_interleaved_comments():
    text = (
        ": connected\n\n"
        'event: simulation.tick\ndata: {"tick": 1}\n\n'
        ": keepalive\n\n"
        'event: allocation.status_changed\ndata: {"id": 2, "status": "PENDING"}\n\n'
        'event: inventory.updated\ndata: {"entity_type": "depot"}\n\n'
    )
    events = feed_all(text)
    assert [e.name for e in events] == ["simulation.tick", "allocation.status_changed", "inventory.updated"]
    assert events[1].data["id"] == 2


def test_multiline_data_crlf_and_non_json():
    p = SSEParser()
    lines = ["event: simulator.notice", "data: hello", "data: world", ""]
    out = [e for line in lines if (e := p.feed(line + "\r")) is not None]
    assert out[0].data == "hello\nworld"


def test_default_event_name_is_message_and_unknown_fields_ignored():
    [e] = feed_all('id: 9\nretry: 100\ndata: {"a": 1}\n\n')
    assert e.name == "message" and e.data == {"a": 1}


async def _aiter(lines):
    for line in lines:
        yield line


async def test_parse_lines_reports_activity_for_every_line():
    seen = []
    events = [
        e
        async for e in parse_lines(
            _aiter([": connected", "", ": keepalive", "", "event: x", 'data: {"k": 1}', ""]),
            lambda: seen.append(1),
        )
    ]
    assert len(seen) == 7 and [e.name for e in events] == ["x"]


@respx.mock
async def test_stream_end_to_end_via_client():
    body = (
        b": connected\n\n"
        b'event: simulation.tick\ndata: {"tick": 1, "sim_time": "2026-01-01T00:15:00"}\n\n'
        b": keepalive\n\n"
        b'event: simulation.tick\ndata: {"tick": 2, "sim_time": "2026-01-01T00:30:00"}\n\n'
    )
    respx.get(f"{BASE}/v1/stream").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    client = SimClient(BASE)
    try:
        events = [e async for e in client.stream()]
    finally:
        await client.aclose()
    assert [e.data["tick"] for e in events] == [1, 2]
    assert client.last_stream_activity is not None


@respx.mock
@pytest.mark.parametrize(
    "body", [{"detail": {"code": "FAULT_INJECTED"}}, {"error": {"code": "FAULT_INJECTED", "message": "x"}}]
)
async def test_503_on_connect_raises_stream_fault(body):
    respx.get(f"{BASE}/v1/stream").mock(return_value=httpx.Response(503, json=body))
    client = SimClient(BASE)
    try:
        with pytest.raises(StreamFaultError):
            async for _ in client.stream():
                pass
    finally:
        await client.aclose()


@respx.mock
async def test_read_timeout_becomes_stream_timeout_error():
    respx.get(f"{BASE}/v1/stream").mock(side_effect=httpx.ReadTimeout("silence"))
    client = SimClient(BASE)
    try:
        with pytest.raises(StreamTimeoutError):
            async for _ in client.stream(read_timeout=0.1):
                pass
    finally:
        await client.aclose()
