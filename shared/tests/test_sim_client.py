import json

import httpx
import pytest
import respx

from fsp_shared.exceptions import SimAPIError, SimPayloadError, SimTransportError
from fsp_shared.schemas import AllocationRequest, SimErrorKind, SimulatorState
from fsp_shared.sim_client import SimClient, backoff_delay, parse_sim_error

BASE = "http://sim.test"

INSTANCE = {
    "id": 1,
    "scenario_id": "baseline",
    "scenario_version": "1.0",
    "seed": 12345,
    "sim_time": "2026-01-01T00:00:00",
    "tick": 7,
    "tick_minutes": 15,
    "status": "PAUSED",
}
ALLOCATION = {
    "id": 1,
    "idempotency_key": "fsp-abc",
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "route_id": "route-gazipur-mirpur",
    "fuel_type": "DIESEL",
    "quantity": 3000,
    "created_tick": 5,
    "departure_tick": None,
    "expected_arrival_tick": None,
    "actual_arrival_tick": None,
    "status": "PENDING",
    "failure_reason": None,
}
FAULT_BODY = {"error": {"code": "FAULT_INJECTED", "message": "Simulator API temporarily unavailable."}}


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
async def client(sleeps):
    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    c = SimClient(BASE, 3.0, sleep=fake_sleep)
    yield c
    await c.aclose()


def request_body() -> AllocationRequest:
    return AllocationRequest(
        idempotency_key="fsp-abc",
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=3000,
    )


# --- stale header ---------------------------------------------------------------------------
@respx.mock
async def test_stale_header_detected(client):
    respx.get(f"{BASE}/v1/instance").mock(
        return_value=httpx.Response(200, json=INSTANCE, headers={"X-Simulator-Stale": "true"})
    )
    resp = await client.instance()
    assert resp.stale is True
    assert resp.data.tick == 7 and resp.tick_hint == 7
    assert client.last_stale is True


@respx.mock
async def test_not_stale_by_default_and_clears_flag(client):
    route = respx.get(f"{BASE}/v1/instance")
    route.side_effect = [
        httpx.Response(200, json=INSTANCE, headers={"X-Simulator-Stale": "true"}),
        httpx.Response(200, json=INSTANCE),
    ]
    assert (await client.instance()).stale is True
    assert (await client.instance()).stale is False
    assert client.last_stale is False


@respx.mock
async def test_naive_sim_time_is_parsed_as_utc(client):
    respx.get(f"{BASE}/v1/instance").mock(return_value=httpx.Response(200, json=INSTANCE))
    data = (await client.instance()).data
    assert data.sim_time.utcoffset().total_seconds() == 0 and data.sim_time.hour == 0


# --- three error formats (C16) ----------------------------------------------------------------
def test_parse_error_fault_injected_shape():
    err = parse_sim_error(httpx.Response(503, json=FAULT_BODY))
    assert (err.code, err.kind, err.status) == ("FAULT_INJECTED", SimErrorKind.FAULT, 503)


def test_parse_error_detail_dict_domain():
    err = parse_sim_error(
        httpx.Response(409, json={"detail": {"code": "ROUTE_DISRUPTED", "message": "nope"}})
    )
    assert (err.code, err.message, err.kind) == ("ROUTE_DISRUPTED", "nope", SimErrorKind.DOMAIN)


def test_parse_error_detail_dict_stream_fault():
    err = parse_sim_error(httpx.Response(503, json={"detail": {"code": "FAULT_INJECTED"}}))
    assert err.kind is SimErrorKind.FAULT and err.code == "FAULT_INJECTED"


def test_parse_error_pydantic_422():
    body = {"detail": [{"loc": ["body", "quantity"], "msg": "Input should be greater than 0", "type": "x"}]}
    err = parse_sim_error(httpx.Response(422, json=body))
    assert err.kind is SimErrorKind.VALIDATION and err.code == "VALIDATION_ERROR"
    assert "body.quantity" in err.message


def test_parse_error_unknown_shapes():
    assert parse_sim_error(httpx.Response(500, text="boom")).kind is SimErrorKind.UNKNOWN
    assert parse_sim_error(httpx.Response(404, json={"detail": "Not Found"})).kind is SimErrorKind.UNKNOWN


# --- retry policy: GET ----------------------------------------------------------------------------
@respx.mock
async def test_get_retries_503_then_succeeds(client, sleeps):
    route = respx.get(f"{BASE}/v1/instance")
    route.side_effect = [httpx.Response(503, json=FAULT_BODY)] * 2 + [httpx.Response(200, json=INSTANCE)]
    resp = await client.instance()
    assert resp.status == 200 and route.call_count == 3 and len(sleeps) == 2
    assert client.consecutive_v1_failures == 0


@respx.mock
async def test_get_gives_up_after_four_attempts(client):
    route = respx.get(f"{BASE}/v1/instance").mock(return_value=httpx.Response(503, json=FAULT_BODY))
    with pytest.raises(SimAPIError) as exc:
        await client.instance()
    assert route.call_count == 4
    assert exc.value.error.kind is SimErrorKind.FAULT


@respx.mock
async def test_get_retries_timeouts_and_connection_errors(client):
    route = respx.get(f"{BASE}/v1/routes")
    route.side_effect = [httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.Response(200, json=[])]
    assert (await client.routes()).data == []
    assert route.call_count == 3


@respx.mock
async def test_transport_error_after_all_retries(client):
    route = respx.get(f"{BASE}/v1/routes").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(SimTransportError):
        await client.routes()
    assert route.call_count == 4


@respx.mock
@pytest.mark.parametrize(
    "status,body",
    [(404, {"detail": {"code": "NOT_FOUND"}}), (409, {"detail": {"code": "X"}}), (422, {"detail": []})],
)
async def test_4xx_not_retried_on_get(client, status, body):
    route = respx.get(f"{BASE}/v1/depots/depot-x").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(SimAPIError) as exc:
        await client.depot("depot-x")
    assert route.call_count == 1 and exc.value.status == status


# --- retry policy: POST allocation ------------------------------------------------------------------
@respx.mock
async def test_post_retries_keep_identical_body_and_key(client):
    route = respx.post(f"{BASE}/v1/allocations")
    route.side_effect = [
        httpx.Response(503, json=FAULT_BODY),
        httpx.ConnectTimeout("t"),
        httpx.Response(503, json=FAULT_BODY),
        httpx.Response(201, json=ALLOCATION),
    ]
    resp = await client.post_allocation(request_body())
    assert resp.status == 201 and resp.data.id == 1
    assert route.call_count == 4
    bodies = [c.request.content for c in route.calls]
    assert len(set(bodies)) == 1  # byte-identical on every attempt
    assert json.loads(bodies[0])["idempotency_key"] == "fsp-abc"


@respx.mock
async def test_post_max_five_attempts(client):
    route = respx.post(f"{BASE}/v1/allocations").mock(return_value=httpx.Response(503, json=FAULT_BODY))
    with pytest.raises(SimAPIError):
        await client.post_allocation(request_body())
    assert route.call_count == 5


@respx.mock
@pytest.mark.parametrize("status", [200, 201])
async def test_post_accepts_200_and_201(client, status):
    respx.post(f"{BASE}/v1/allocations").mock(return_value=httpx.Response(status, json=ALLOCATION))
    assert (await client.post_allocation(request_body())).status == status


@respx.mock
@pytest.mark.parametrize(
    "status,body,code",
    [
        (409, {"detail": {"code": "IDEMPOTENCY_KEY_MISMATCH", "message": "m"}}, "IDEMPOTENCY_KEY_MISMATCH"),
        (
            409,
            {"detail": {"code": "DISPATCH_CAPACITY_EXCEEDED", "message": "m"}},
            "DISPATCH_CAPACITY_EXCEEDED",
        ),
        (404, {"detail": {"code": "NOT_FOUND", "message": "m"}}, "NOT_FOUND"),
        (422, {"detail": [{"loc": ["body", "fuel_type"], "msg": "bad", "type": "x"}]}, "VALIDATION_ERROR"),
    ],
)
async def test_post_4xx_not_retried(client, status, body, code):
    route = respx.post(f"{BASE}/v1/allocations").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(SimAPIError) as exc:
        await client.post_allocation(request_body())
    assert route.call_count == 1 and exc.value.code == code


@respx.mock
async def test_cancel_allocation(client):
    respx.post(f"{BASE}/v1/allocations/1/cancel").mock(
        return_value=httpx.Response(200, json={**ALLOCATION, "status": "CANCELLED"})
    )
    assert (await client.cancel_allocation(1)).data.status == "CANCELLED"


@respx.mock
async def test_cancel_conflict_is_domain_error(client):
    route = respx.post(f"{BASE}/v1/allocations/9/cancel").mock(
        return_value=httpx.Response(409, json={"detail": {"code": "CANNOT_CANCEL", "message": "m"}})
    )
    with pytest.raises(SimAPIError) as exc:
        await client.cancel_allocation(9)
    assert exc.value.code == "CANNOT_CANCEL" and route.call_count == 1


def test_allocation_request_validates_client_side():
    with pytest.raises(ValueError):
        AllocationRequest(
            idempotency_key="",
            source_depot_id="a",
            destination_station_id="b",
            route_id="c",
            fuel_type="DIESEL",
            quantity=1,
        )
    with pytest.raises(ValueError):
        AllocationRequest(
            idempotency_key="k" * 151,
            source_depot_id="a",
            destination_station_id="b",
            route_id="c",
            fuel_type="DIESEL",
            quantity=1,
        )
    with pytest.raises(ValueError):
        AllocationRequest(
            idempotency_key="k",
            source_depot_id="a",
            destination_station_id="b",
            route_id="c",
            fuel_type="KEROSENE",
            quantity=1,
        )
    with pytest.raises(ValueError):
        AllocationRequest(
            idempotency_key="k",
            source_depot_id="a",
            destination_station_id="b",
            route_id="c",
            fuel_type="DIESEL",
            quantity=0,
        )


# --- simulator state --------------------------------------------------------------------------------
@respx.mock
async def test_state_up_degraded_down(client):
    health = respx.get(f"{BASE}/v1/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    inst = respx.get(f"{BASE}/v1/instance").mock(return_value=httpx.Response(200, json=INSTANCE))
    await client.instance()
    assert await client.probe_state() is SimulatorState.UP

    inst.mock(return_value=httpx.Response(503, json=FAULT_BODY))
    with pytest.raises(SimAPIError):
        await client.instance()
    assert await client.probe_state() is SimulatorState.DEGRADED  # /v1/* failing, health fine

    health.mock(side_effect=httpx.ConnectError("down"))
    assert await client.probe_state() is SimulatorState.DOWN

    health.mock(return_value=httpx.Response(200, json={"status": "ok"}))
    inst.mock(return_value=httpx.Response(200, json=INSTANCE))
    await client.instance()
    assert await client.probe_state() is SimulatorState.UP


@respx.mock
async def test_health_is_not_retried(client):
    route = respx.get(f"{BASE}/v1/health").mock(return_value=httpx.Response(503, json=FAULT_BODY))
    with pytest.raises(SimAPIError):
        await client.health()
    assert route.call_count == 1


# --- malformed payloads --------------------------------------------------------------------------------
@respx.mock
async def test_malformed_payload_rejected(client):
    bad = {**INSTANCE, "status": "EXPLODING"}
    respx.get(f"{BASE}/v1/instance").mock(return_value=httpx.Response(200, json=bad))
    with pytest.raises(SimPayloadError):
        await client.instance()
    respx.get(f"{BASE}/v1/routes").mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(SimPayloadError):
        await client.routes()


@respx.mock
async def test_demand_history_always_sends_clamped_limit(client):
    route = respx.get(f"{BASE}/v1/demand-history").mock(return_value=httpx.Response(200, json=[]))
    await client.demand_history("station-tongi", 999_999)
    assert route.calls.last.request.url.params["limit"] == "2000"
    assert route.calls.last.request.url.params["station_id"] == "station-tongi"
    await client.demand_history(limit=-5)
    assert route.calls.last.request.url.params["limit"] == "1"


@respx.mock
async def test_typed_models_for_world_endpoints(client):
    respx.get(f"{BASE}/v1/depots").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "depot-gazipur",
                    "name": "G",
                    "region_id": "region-dhaka",
                    "status": "OPEN",
                    "dispatch_capacity_per_tick": 12000,
                    "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                    "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
                }
            ],
        )
    )
    depots = (await client.depots()).data
    assert depots[0].inventory["DIESEL"] == 60000 and depots[0].status == "OPEN"


# --- metrics -----------------------------------------------------------------------------------------------
@respx.mock
async def test_metrics_recorded(client):
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("sim_faults_detected_total", {"type": "fault"}) or 0
    stale_before = REGISTRY.get_sample_value("sim_faults_detected_total", {"type": "stale"}) or 0
    respx.get(f"{BASE}/v1/metrics").mock(
        side_effect=[
            httpx.Response(503, json=FAULT_BODY),
            httpx.Response(
                200,
                json={
                    "served_demand_liters": 1,
                    "unmet_demand_liters": 0,
                    "service_level": 1.0,
                    "allocation_liters": 0,
                    "allocation_failures": 0,
                },
                headers={"X-Simulator-Stale": "true"},
            ),
        ]
    )
    await client.metrics()
    assert (REGISTRY.get_sample_value("sim_faults_detected_total", {"type": "fault"}) or 0) == before + 1
    assert (
        REGISTRY.get_sample_value("sim_faults_detected_total", {"type": "stale"}) or 0
    ) == stale_before + 1
    assert (
        REGISTRY.get_sample_value(
            "sim_client_requests_total", {"endpoint": "/v1/metrics", "method": "GET", "status": "503"}
        )
        >= 1
    )


def test_backoff_is_full_jitter_and_capped():
    for attempt in range(10):
        for _ in range(50):
            d = backoff_delay(attempt)
            assert 0.0 <= d <= min(2.0, 0.1 * 2**attempt)
    assert max(backoff_delay(9) for _ in range(200)) > 1.0  # actually reaches the upper range
