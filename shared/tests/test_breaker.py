import asyncio

import pytest
from prometheus_client import REGISTRY

from fsp_shared.breaker import BreakerState, CircuitBreaker
from fsp_shared.exceptions import BreakerOpenError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gauge(name):
    return REGISTRY.get_sample_value("breaker_state", {"dependency": name})


async def boom():
    raise RuntimeError("upstream down")


async def ok():
    return "fine"


async def test_opens_after_threshold_consecutive_failures():
    clock = Clock()
    b = CircuitBreaker("t_open", failure_threshold=5, open_seconds=30, clock=clock)
    for i in range(4):
        with pytest.raises(RuntimeError):
            await b.call(boom)
        assert b.state is BreakerState.CLOSED, i
    with pytest.raises(RuntimeError):
        await b.call(boom)
    assert b.state is BreakerState.OPEN and gauge("t_open") == 2
    with pytest.raises(BreakerOpenError):
        await b.call(ok)  # rejected without calling


async def test_success_resets_consecutive_count():
    b = CircuitBreaker("t_reset", failure_threshold=3, clock=Clock())
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await b.call(boom)
    assert await b.call(ok) == "fine"
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await b.call(boom)
    assert b.state is BreakerState.CLOSED  # 2 + 2 failures, never 3 in a row


async def test_recovers_via_half_open_probe():
    clock = Clock()
    b = CircuitBreaker("t_recover", failure_threshold=2, open_seconds=30, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await b.call(boom)
    assert b.state is BreakerState.OPEN
    clock.t += 29
    assert b.state is BreakerState.OPEN
    clock.t += 2
    assert b.state is BreakerState.HALF_OPEN and gauge("t_recover") == 1
    assert await b.call(ok) == "fine"
    assert b.state is BreakerState.CLOSED and gauge("t_recover") == 0


async def test_failed_probe_reopens_and_restarts_the_timer():
    clock = Clock()
    b = CircuitBreaker("t_probe_fail", failure_threshold=1, open_seconds=10, clock=clock)
    with pytest.raises(RuntimeError):
        await b.call(boom)
    clock.t += 11
    assert b.state is BreakerState.HALF_OPEN
    with pytest.raises(RuntimeError):
        await b.call(boom)
    assert b.state is BreakerState.OPEN
    clock.t += 5
    assert b.state is BreakerState.OPEN  # timer restarted at the failed probe
    clock.t += 6
    assert b.state is BreakerState.HALF_OPEN


async def test_half_open_admits_only_one_probe():
    clock = Clock()
    b = CircuitBreaker("t_one_probe", failure_threshold=1, open_seconds=10, clock=clock)
    with pytest.raises(RuntimeError):
        await b.call(boom)
    clock.t += 11
    assert b.allow() is True  # the probe
    assert b.allow() is False  # everyone else keeps using the fallback
    b.record_success()
    assert b.allow() is True


async def test_timeout_counts_as_failure():
    b = CircuitBreaker("t_timeout", failure_threshold=2, timeout=0.01, clock=Clock())

    async def slow():
        await asyncio.sleep(1)

    for _ in range(2):
        with pytest.raises(asyncio.TimeoutError):
            await b.call(slow)
    assert b.state is BreakerState.OPEN


async def test_fallback_counter_increments_when_open():
    b = CircuitBreaker("t_fallback", failure_threshold=1, clock=Clock())
    with pytest.raises(RuntimeError):
        await b.call(boom)
    before = (
        REGISTRY.get_sample_value(
            "fallback_activations_total", {"dependency": "t_fallback", "reason": "breaker_open"}
        )
        or 0
    )
    with pytest.raises(BreakerOpenError):
        await b.call(ok)
    after = REGISTRY.get_sample_value(
        "fallback_activations_total", {"dependency": "t_fallback", "reason": "breaker_open"}
    )
    assert after == before + 1
    b.record_fallback("custom")
    assert (
        REGISTRY.get_sample_value(
            "fallback_activations_total", {"dependency": "t_fallback", "reason": "custom"}
        )
        == 1
    )
