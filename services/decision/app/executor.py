"""Executor with outbox (spec §11.9). The decision row is moved to COMMITTING *before* the POST; the idempotency key
is ``fsp-<decision_id>`` and the body never changes for that key. A reconciler retries COMMITTING rows (also on
service start) — the simulator deduplicates, so a crash between POST and bookkeeping cannot double-dispatch."""

from __future__ import annotations

import time

from fsp_shared.exceptions import SimAPIError, SimClientError
from fsp_shared.logging import bind_context, get_logger
from fsp_shared.schemas import AllocationRequest
from fsp_shared.sim_client import SimClient

from .metrics import ALLOC_COMMITTED, ALLOC_REJECTED, EXECUTOR_LATENCY
from .repo import Repo
from .validator import Candidate, validate
from .world import World

log = get_logger("decision.executor")
APPROVED = ("AUTO_APPROVED", "OPERATOR_APPROVED")


class Executor:
    def __init__(self, client: SimClient, repo: Repo, derate: float) -> None:
        self.client = client
        self.repo = repo
        self.derate = derate

    @staticmethod
    def request_for(d: dict) -> AllocationRequest:
        return AllocationRequest(
            idempotency_key=d["idempotency_key"], source_depot_id=d["source_depot_id"],
            destination_station_id=d["station_id"], route_id=d["route_id"], fuel_type=d["fuel_type"],
            quantity=float(d["quantity"]),
        )

    async def execute(self, d: dict, world: World | None, *, preflight: bool = True) -> str:
        """Returns the resulting status. ``world`` = freshest state for the pre-flight re-check."""
        did = d["decision_id"]
        bind_context(decision_id=did, idempotency_key=d["idempotency_key"])
        if d["status"] in APPROVED:
            if not await self.repo.transition(did, APPROVED, "COMMITTING"):
                return "SKIPPED"  # someone else took it
        if preflight and world is not None:
            code = validate(world, Candidate(d["source_depot_id"], d["station_id"], d["route_id"], d["fuel_type"],
                                             float(d["quantity"]), exclude_decision_id=did), derate=self.derate)
            if code != "OK":
                await self.repo.update_decision(did, status="SIM_REJECTED", sim_error_code=f"PREFLIGHT_{code}")
                ALLOC_REJECTED.labels(f"PREFLIGHT_{code}").inc()
                await self.repo.audit(tick=world.tick, actor="decision-svc", action="allocation.preflight_rejected",
                                      entity_type="decision", entity_id=did, result=code)
                return "SIM_REJECTED"
        started = time.perf_counter()
        try:
            resp = await self.client.post_allocation(self.request_for(d))
        except SimAPIError as exc:
            EXECUTOR_LATENCY.observe(time.perf_counter() - started)
            return await self._on_error(d, exc, world)
        except SimClientError as exc:  # 503s / timeouts exhausted: stay COMMITTING, reconciler retries same key
            log.warning("allocation_post_deferred", error=str(exc)[:200])
            return "COMMITTING"
        EXECUTOR_LATENCY.observe(time.perf_counter() - started)
        await self.repo.update_decision(did, status="COMMITTED", sim_allocation_id=resp.data.id, sim_error_code=None)
        ALLOC_COMMITTED.labels(d["system_origin"]).inc()
        await self.repo.audit(tick=world.tick if world else None, actor="decision-svc", action="allocation.executed",
                              entity_type="decision", entity_id=did, result=f"HTTP {resp.status}",
                              data={"sim_allocation_id": resp.data.id, "qty": d["quantity"], "route": d["route_id"],
                                    "origin": d["system_origin"]})
        log.info("allocation_committed", sim_allocation_id=resp.data.id, qty=d["quantity"], route=d["route_id"])
        return "COMMITTED"

    async def _on_error(self, d: dict, exc: SimAPIError, world: World | None) -> str:
        did, code, tick = d["decision_id"], exc.code, world.tick if world else None
        ALLOC_REJECTED.labels(code).inc()
        if code == "DISPATCH_CAPACITY_EXCEEDED":  # try again next tick with the same key and body
            await self.repo.update_decision(did, status="AUTO_APPROVED" if d["system_origin"] != "OPERATOR_MANUAL"
                                            and d.get("operator") is None else "OPERATOR_APPROVED", sim_error_code=code)
            return "APPROVED_RETRY"
        await self.repo.update_decision(did, status="SIM_REJECTED", sim_error_code=code)
        severity = "CRITICAL" if code in ("IDEMPOTENCY_KEY_MISMATCH",) or exc.status == 422 else "HIGH"
        kind = {"IDEMPOTENCY_KEY_MISMATCH": "BUG_IDEMPOTENCY", "NOT_FOUND": "CONFIG_ERROR"}.get(code, "ALLOCATION_REJECTED")
        if exc.status == 422:
            kind = "BUG_VALIDATION"
        await self.repo.alert(tick=tick, kind=kind, severity=severity, message=f"Simulator rejected {did}: {code}",
                              entity_id=d["station_id"], fuel=d["fuel_type"], data={"message": exc.error.message})
        await self.repo.audit(tick=tick, actor="decision-svc", action="allocation.rejected", entity_type="decision",
                              entity_id=did, result=code)
        return "SIM_REJECTED"

    async def reconcile(self, world: World | None) -> int:
        """Retry every COMMITTING decision with its original key/body (no pre-flight: it may already exist)."""
        n = 0
        for d in await self.repo.by_status("COMMITTING"):
            if await self.execute(d, world, preflight=False) == "COMMITTED":
                n += 1
        return n
