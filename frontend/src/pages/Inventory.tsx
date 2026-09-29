import { Boxes, Droplets, Route } from "lucide-react";
import { useApi } from "@/lib/api";
import { Badge, Card, CardTitle, Gauge, PageHeader, Status } from "@/components/ui";
import { fmtL } from "@/lib/utils";

export default function Inventory() {
  const st = useApi<any[]>("stations", "/api/stations", 3000);
  const dp = useApi<any[]>("depots", "/api/depots", 3000);
  return (
    <>
      <PageHeader title="Inventory" sub="Tank levels per depot and station · blue = on hand, light blue = in flight, red mark = safety stock" />
      <h2 className="font-semibold text-lg">Depots</h2>
      <div className="grid md:grid-cols-2 gap-6">
        {(dp.data ?? []).map((d) => (
          <Card key={d.id}>
            <CardTitle icon={<Boxes className="text-blue-600" />} right={<Status s={d.status} />}>{d.name}</CardTitle>
            <p className="text-xs text-muted-foreground -mt-2">{d.region_id} · dispatch {fmtL(d.dispatch_capacity_per_tick)}/tick{d.status === "CONSTRAINED" ? " (derated)" : ""}</p>
            {Object.entries<any>(d.fuels).map(([f, v]) => (
              <div key={f}>
                <Gauge label={f} inv={v.inventory} cap={v.capacity} />
                <p className="text-[11px] text-muted-foreground mt-0.5">pending out {fmtL(v.pending_out)} · reserved {fmtL(v.reserved)}
                  {v.next_supply ? ` · next supply ${fmtL(v.next_supply.quantity)} @ tick ${v.next_supply.planned_tick} (${v.next_supply.status})` : ""}</p>
              </div>
            ))}
          </Card>
        ))}
      </div>
      <h2 className="font-semibold text-lg">Stations</h2>
      <div className="grid md:grid-cols-2 gap-6">
        {(st.data ?? []).map((s) => (
          <Card key={s.id}>
            <CardTitle icon={<Droplets className="text-green-600" />} right={<div className="flex gap-1">{s.demand_multiplier !== 1 && <Badge tone="purple">demand ×{s.demand_multiplier}</Badge>}<Status s={s.status} /></div>}>{s.name}</CardTitle>
            <p className="text-xs text-muted-foreground -mt-2 flex items-center gap-1"><Route className="size-3" />{s.routes.join(", ")}{s.single_route ? " · single route (no alternate path)" : ""}</p>
            {Object.entries<any>(s.fuels).map(([f, v]) => (
              <div key={f}>
                <Gauge label={f} inv={v.inventory} cap={v.capacity} inflight={v.in_flight + v.reserved} safety={v.safety_stock} />
                <p className="text-[11px] text-muted-foreground mt-0.5">burn {v.burn_rate_lph != null ? `${Math.round(v.burn_rate_lph)} L/h` : "—"} · empty in {v.t_empty_hours != null ? `${v.t_empty_hours} h` : "> horizon"} · staged {fmtL(v.reserved)}</p>
              </div>
            ))}
          </Card>
        ))}
      </div>
    </>
  );
}
