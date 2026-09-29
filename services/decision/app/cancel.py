"""Cancel doomed PENDING allocations (spec §11.10): route DISRUPTED now or a disruption covers the departure tick."""

from __future__ import annotations

from fsp_shared.exceptions import SimAPIError, SimClientError
from fsp_shared.logging import get_logger
from fsp_shared.sim_client import SimClient

from .metrics import CANCELLATIONS
from .repo import Repo
from .validator import route_disrupted_at
from .world import World

log = get_logger("decision.cancel")


async def cancel_doomed(world: World, client: SimClient, repo: Repo) -> list[int]:
    cancelled = []
    for a in world.allocations:
        if a.status != "PENDING":
            continue
        dep = a.departure_tick if a.departure_tick is not None else (a.created_tick or world.tick) + 1
        route = world.routes.get(a.route_id)
        if not ((route and route.status != "AVAILABLE") or route_disrupted_at(world, a.route_id, dep)):
            continue
        try:
            await client.cancel_allocation(a.id)
            result = "CANCELLED"
            cancelled.append(a.id)
        except SimAPIError as exc:
            result = exc.code  # CANNOT_CANCEL = already departed: fine
        except SimClientError as exc:
            result = "ERROR"
            log.warning("cancel_failed", allocation_id=a.id, error=str(exc)[:150])
        CANCELLATIONS.labels(result).inc()
        async with repo.engine.begin() as c:
            from sqlalchemy import text

            if result == "CANCELLED":
                await c.execute(text("UPDATE decisions SET status = 'CANCELLED', updated_at = now() "
                                     "WHERE sim_allocation_id = :id"), {"id": a.id})
        await repo.audit(tick=world.tick, actor="decision-svc", action="allocation.cancel", entity_type="allocation",
                         entity_id=str(a.id), result=result, data={"route_id": a.route_id, "reason": "route disruption"})
    return cancelled
