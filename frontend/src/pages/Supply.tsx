import { AlertOctagon, CalendarClock, FileWarning, Route } from "lucide-react";
import { useApi } from "@/lib/api";
import { Badge, Card, CardTitle, Empty, PageHeader, Status, Table } from "@/components/ui";
import { fmtL } from "@/lib/utils";

export default function Supply() {
  const sup = useApi<any[]>("supply", "/api/supply", 5000);
  const ev = useApi<any[]>("events", "/api/events", 3000);
  const rt = useApi<any[]>("routes", "/api/routes", 3000);
  const inc = useApi<any[]>("incidents", "/api/incidents", 5000);
  const tick = useApi("overview", "/api/overview", 2000).data?.tick?.tick ?? 0;
  const params = (p: any) => Object.entries(p || {}).map(([k, v]) => `${k}=${Array.isArray(v) ? (v.length ? v.join(",") : "all") : v}`).join(" · ");

  return (
    <>
      <PageHeader title="Supply & Disruptions" sub="Depot supply arrivals, route status, simulator events and incident briefs" />
      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <CardTitle icon={<Route className="text-blue-600" />}>Route Status</CardTitle>
          <Table head={["Route", "Depot → Station", "Transit", "Max shipment", "Status"]}>
            {(rt.data ?? []).map((r) => (
              <tr key={r.route_id}><td className="font-medium">{r.route_id.replace("route-", "")}</td><td>{r.depot_name} → {r.station_name}</td>
                <td>{r.transit_ticks} ticks</td><td className="tabular">{fmtL(r.max_shipment)}</td>
                <td><div className="flex flex-wrap gap-1"><Status s={r.status} />
                  {r.disruptions?.map((d: any) => <Badge key={d.id} tone="amber">{d.status} t{d.start_tick}–{d.end_tick}</Badge>)}
                  {r.status !== "AVAILABLE" && r.single_route_station && <Badge tone="red">no alternate path</Badge>}</div></td></tr>
            ))}
          </Table>
        </Card>
        <Card>
          <CardTitle icon={<AlertOctagon className="text-red-500" />}>Events Feed</CardTitle>
          {(ev.data ?? []).length === 0 && <Empty>No events injected (baseline scenario)</Empty>}
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {(ev.data ?? []).map((e) => (
              <div key={e.id} className="rounded-lg border p-3">
                <div className="flex items-center gap-2"><Status s={e.status} /><span className="font-medium text-sm">{e.type}</span><span className="ml-auto text-xs text-muted-foreground">#{e.id} · ticks {e.start_tick}–{e.end_tick}</span></div>
                <p className="text-xs text-muted-foreground mt-1">{params(e.parameters)}{e.status === "SCHEDULED" ? ` · starts in ${e.start_tick - tick} ticks` : ""}</p>
              </div>
            ))}
          </div>
        </Card>
      </div>
      <Card>
        <CardTitle icon={<CalendarClock className="text-orange-500" />}>Supply Arrival Timeline</CardTitle>
        <Table head={["Arrival", "Depot", "Fuel", "Quantity", "Planned tick", "Actual tick", "Status"]}>
          {(sup.data ?? []).map((s) => (
            <tr key={s.id} className={s.planned_tick < tick && s.status === "ARRIVED" ? "opacity-60" : ""}>
              <td className="font-mono text-xs">{s.id}</td><td>{s.depot_id}</td><td>{s.fuel_type}</td>
              <td className="tabular">{fmtL(s.quantity)}{s.first_quantity && s.quantity < s.first_quantity && <Badge tone="red" className="ml-1">shortfall, was {fmtL(s.first_quantity)}</Badge>}</td>
              <td className="tabular">{s.planned_tick}{s.first_planned_tick != null && s.planned_tick !== s.first_planned_tick && <Badge tone="amber" className="ml-1">+{s.planned_tick - s.first_planned_tick} delay</Badge>}</td>
              <td className="tabular">{s.actual_tick ?? "—"}</td><td><Status s={s.status} /></td></tr>
          ))}
        </Table>
      </Card>
      <Card>
        <CardTitle icon={<FileWarning className="text-purple-600" />}>Incident Briefs</CardTitle>
        {(inc.data ?? []).length === 0 && <Empty>No incidents yet</Empty>}
        <div className="grid md:grid-cols-2 gap-4">
          {(inc.data ?? []).map((i) => {
            const b = i.brief || {};
            return (
              <div key={i.id} className="rounded-lg border p-4 space-y-2">
                <div className="flex items-center gap-2"><span className="font-semibold">{b.title ?? `Incident #${i.id}`}</span><Badge tone={i.source === "TEMPLATE" ? "gray" : "purple"} className="ml-auto">{i.source}</Badge></div>
                <p className="text-sm">{b.impact_summary}</p>
                {b.affected_entities?.length > 0 && <div className="flex flex-wrap gap-1">{b.affected_entities.map((e: string) => <Badge key={e}>{e}</Badge>)}</div>}
                {b.lifelines_at_risk?.length > 0 && <p className="text-xs"><span className="font-medium text-red-600">At risk:</span> {b.lifelines_at_risk.join(", ")}</p>}
                {b.recommended_operator_actions?.length > 0 && <ul className="text-xs list-disc pl-4 text-muted-foreground">{b.recommended_operator_actions.map((a: string, k: number) => <li key={k}>{a}</li>)}</ul>}
              </div>
            );
          })}
        </div>
      </Card>
    </>
  );
}
