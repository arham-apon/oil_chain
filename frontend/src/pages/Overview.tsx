import { useState } from "react";
import { Area, AreaChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, Boxes, CalendarClock, CheckCircle2, ClipboardList, Droplets, Globe, MapPin, RefreshCw, ShieldAlert, Truck, TrendingUp, XCircle, Zap } from "lucide-react";
import { useApi } from "@/lib/api";
import { Badge, Button, Card, CardTitle, Empty, PageHeader, Progress, StatCard, Status, statusTone, Tabs } from "@/components/ui";
import { POS } from "@/config/network";
import { cn, fmtL, pct } from "@/lib/utils";

export default function Overview() {
  const ov = useApi("overview", "/api/overview", 2000);
  const st = useApi<any[]>("stations", "/api/stations", 3000);
  const dp = useApi<any[]>("depots", "/api/depots", 3000);
  const rt = useApi<any[]>("routes", "/api/routes", 3000);
  const sup = useApi<any[]>("supply", "/api/supply", 8000);
  const al = useApi<any[]>("allocations", "/api/allocations?limit=300", 4000);
  const [range, setRange] = useState("1D");
  const hist = useApi<any[]>("metrics", `/api/metrics/history?limit=${{ "6H": 24, "1D": 96, "3D": 288 }[range]}`, 5000);
  const o = ov.data, k = o?.kpis, p = o?.kpis_prev_day, c = o?.counts;
  const tick = o?.tick?.tick ?? 0;
  const refresh = () => [ov, st, dp, rt, sup, al, hist].forEach((q) => q.refetch());

  const upcoming = (sup.data ?? []).filter((s) => s.status !== "ARRIVED").slice(0, 4);
  const risks = (st.data ?? []).flatMap((s) => Object.entries<any>(s.fuels).map(([f, v]) => ({ id: `${s.name} ${f}`, ...v })))
    .sort((a, b) => (b.stockout_risk ?? 0) - (a.stockout_risk ?? 0)).slice(0, 5);
  const byStatus = ["PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED"].map((s) => ({
    name: s, value: (al.data ?? []).filter((a) => a.status === s).length,
  }));
  const PIE = ["#f97316", "#3b82f6", "#10b981", "#ef4444", "#9ca3af"];
  const chart = (hist.data ?? []).map((h, i, arr) => ({
    tick: h.tick, sl: +(h.service_level * 100).toFixed(2),
    unmet: i ? Math.max(0, h.unmet - arr[i - 1].unmet) : 0,
  }));

  return (
    <>
      <PageHeader title="Operations Overview" sub="Real-time fuel supply network state · all data is simulated">
        <Badge tone="gray"><CalendarClock className="size-3" />Tick {tick}</Badge>
        <Button onClick={refresh}><RefreshCw />Refresh</Button>
      </PageHeader>

      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3">
        <StatCard icon={<CheckCircle2 />} color="bg-green-500" value={pct(k?.service_level, 2)} label="Service Level" sub="served / total demand"
          delta={p ? `${((k.service_level - p.service_level) * 100).toFixed(2)} pts` : undefined} deltaGood={!p || k.service_level >= p.service_level} progress={(k?.service_level ?? 0) * 100} />
        <StatCard icon={<XCircle />} color="bg-red-500" value={fmtL(k?.unmet)} label="Unmet Demand" sub="cumulative" delta={p ? `+${Math.round(k.unmet - p.unmet).toLocaleString()} /day` : undefined} deltaGood={!p || k.unmet - p.unmet < 1} />
        <StatCard icon={<Droplets />} color="bg-blue-500" value={fmtL(k?.served)} label="Served Demand" sub="cumulative" />
        <StatCard icon={<Truck />} color="bg-purple-500" value={c?.in_flight ?? "—"} label="In-flight Allocations" sub={fmtL(c?.in_flight_liters)} />
        <StatCard icon={<Boxes />} color="bg-teal-500" value={fmtL(k?.allocation_liters)} label="Allocated" sub="liters dispatched" />
        <StatCard icon={<AlertTriangle />} color="bg-orange-500" value={k?.allocation_failures ?? "—"} label="Allocation Failures" sub="FAILED in simulator" deltaGood={!k?.allocation_failures} delta={k?.allocation_failures ? "check routes" : "none"} />
        <StatCard icon={<ClipboardList />} color="bg-indigo-500" value={c?.staged ?? "—"} label="Staged Decisions" sub="awaiting operator" />
        <StatCard icon={<ShieldAlert />} color="bg-rose-500" value={c?.alerts_open ?? "—"} label="Open Alerts" sub={`${c?.active_events ?? 0} active events · ${c?.disrupted_routes ?? 0} routes down`} deltaGood={!c?.alerts_high} delta={c?.alerts_high ? `${c.alerts_high} high` : undefined} />
      </div>

      <Card>
        <CardTitle icon={<Globe className="text-blue-600" />} right={<span className="text-xs text-muted-foreground">Positions approximate, display only</span>}>Network Map</CardTitle>
        <div className="grid lg:grid-cols-[1fr_320px] gap-4">
          <NetworkMap stations={st.data ?? []} depots={dp.data ?? []} routes={rt.data ?? []} />
          <div className="space-y-2 max-h-[420px] overflow-y-auto pr-1">
            <p className="text-sm font-semibold">Stations ({st.data?.length ?? 0})</p>
            {(st.data ?? []).map((s) => {
              const worst = Math.max(...Object.values<any>(s.fuels).map((f) => f.stockout_risk ?? 0));
              return (
                <div key={s.id} className="rounded-lg border p-3 space-y-2">
                  <div className="flex items-center justify-between"><span className="font-semibold text-sm flex items-center gap-1"><MapPin className="size-3.5" />{s.name}</span><Status s={s.status} /></div>
                  <div className="flex flex-wrap gap-1 text-xs">
                    <Badge tone={worst >= 0.5 ? "red" : worst >= 0.2 ? "amber" : "green"}>risk {pct(worst, 0)}</Badge>
                    {s.single_route && <Badge tone="gray">single route</Badge>}
                    {s.demand_multiplier !== 1 && <Badge tone="purple"><Zap className="size-3" />×{s.demand_multiplier}</Badge>}
                  </div>
                  {Object.entries<any>(s.fuels).map(([f, v]) => (
                    <div key={f} className="flex items-center gap-2 text-xs"><span className="w-14 text-muted-foreground">{f}</span><Progress value={v.fill * 100} className="h-1.5" bar={v.fill < 0.2 ? "bg-red-500" : "bg-blue-600"} /><span className="w-10 text-right tabular">{Math.round(v.fill * 100)}%</span></div>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      </Card>

      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <CardTitle icon={<CalendarClock className="text-orange-500" />}>Upcoming Depot Supply</CardTitle>
          {upcoming.length === 0 && <Empty>No scheduled arrivals</Empty>}
          {upcoming.map((s) => (
            <div key={s.id} className="flex items-center gap-3 rounded-lg border p-3">
              <div className="h-9 w-9 rounded-lg bg-blue-100 dark:bg-blue-900/40 flex items-center justify-center"><Truck className="size-4 text-blue-600" /></div>
              <div className="flex-1 min-w-0"><p className="font-medium text-sm">{s.depot_id.replace("depot-", "")} · {s.fuel_type} <Status s={s.status} /></p>
                <p className="text-xs text-muted-foreground">{fmtL(s.quantity)}{s.first_quantity && s.quantity < s.first_quantity ? ` (was ${fmtL(s.first_quantity)})` : ""}</p></div>
              <div className="text-right"><p className="font-semibold text-sm tabular">tick {s.planned_tick}</p><p className="text-xs text-muted-foreground">in {s.planned_tick - tick} ticks</p></div>
            </div>
          ))}
        </Card>
        <Card>
          <CardTitle icon={<TrendingUp className="text-green-600" />}>Highest Stockout Risk</CardTitle>
          {risks.map((r) => (
            <div key={r.id} className="space-y-1">
              <div className="flex justify-between text-sm"><span className="font-medium">{r.id}</span><span className={cn("font-semibold", (r.stockout_risk ?? 0) >= 0.5 ? "text-red-600" : (r.stockout_risk ?? 0) >= 0.2 ? "text-orange-600" : "text-green-600")}>{pct(r.stockout_risk)}</span></div>
              <Progress value={(r.stockout_risk ?? 0) * 100} bar={(r.stockout_risk ?? 0) >= 0.5 ? "bg-red-500" : "bg-primary"} />
              <div className="flex justify-between text-xs text-muted-foreground"><span>{fmtL(r.inventory)} on hand</span><span>empty in {r.t_empty_hours != null ? `${r.t_empty_hours} h` : "> horizon"}</span></div>
            </div>
          ))}
          {risks.length === 0 && <Empty>Forecasts not available yet</Empty>}
        </Card>
      </div>

      <div className="grid lg:grid-cols-[2fr_1fr] gap-6">
        <Card>
          <CardTitle icon={<TrendingUp className="text-green-600" />} right={<Tabs tabs={["6H", "1D", "3D"]} value={range} onChange={setRange} />}>Service Level & Unmet Demand</CardTitle>
          <div className="h-72">
            <ResponsiveContainer>
              <AreaChart data={chart}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
                <XAxis dataKey="tick" tick={{ fontSize: 11 }} />
                <YAxis yAxisId="l" domain={["auto", 100]} tick={{ fontSize: 11 }} unit="%" />
                <YAxis yAxisId="r" orientation="right" tick={{ fontSize: 11 }} />
                <Tooltip />
                <Area yAxisId="l" type="monotone" dataKey="sl" name="service level %" stroke="#10b981" fill="#10b981" fillOpacity={0.1} />
                <Area yAxisId="r" type="monotone" dataKey="unmet" name="unmet L / tick" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.1} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>
        <Card>
          <CardTitle icon={<Boxes className="text-blue-600" />}>Allocation Status</CardTitle>
          <div className="h-48"><ResponsiveContainer><PieChart><Pie data={byStatus} dataKey="value" innerRadius={50} outerRadius={80}>{byStatus.map((_, i) => <Cell key={i} fill={PIE[i]} />)}</Pie><Tooltip /></PieChart></ResponsiveContainer></div>
          {byStatus.map((b, i) => (
            <div key={b.name} className="flex items-center justify-between text-sm"><span className="flex items-center gap-2"><span className="h-2.5 w-2.5 rounded-full" style={{ background: PIE[i] }} />{b.name}</span><span className="font-semibold tabular">{b.value}</span></div>
          ))}
        </Card>
      </div>
    </>
  );
}

function NetworkMap({ stations, depots, routes }: { stations: any[]; depots: any[]; routes: any[] }) {
  return (
    <div className="map-grid relative rounded-xl h-[420px] overflow-hidden">
      <svg className="absolute inset-0 w-full h-full" viewBox="0 0 100 100" preserveAspectRatio="none">
        {routes.map((r) => {
          const a = POS[r.depot_id], b = POS[r.station_id];
          if (!a || !b) return null;
          const bad = r.status !== "AVAILABLE";
          return <line key={r.route_id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={bad ? "#ef4444" : r.disruptions?.length ? "#f97316" : "#3b82f6"}
            strokeWidth={0.5} strokeDasharray={bad ? "1.5 1.5" : "2 1"} className={bad ? "" : "route-flow"} vectorEffect="non-scaling-stroke" style={{ strokeWidth: 2.5 }} />;
        })}
      </svg>
      {[...depots.map((d) => ({ ...d, kind: "depot" })), ...stations.map((s) => ({ ...s, kind: "station" }))].map((n) => {
        const p = POS[n.id];
        if (!p) return null;
        const risk = n.kind === "station" ? Math.max(...Object.values<any>(n.fuels).map((f) => f.stockout_risk ?? 0)) : 0;
        const ring = n.status !== "OPEN" ? "ring-red-500" : risk >= 0.5 ? "ring-red-500" : risk >= 0.2 ? "ring-orange-400" : "ring-green-500";
        return (
          <div key={n.id} className="absolute -translate-x-1/2 -translate-y-1/2 flex flex-col items-center" style={{ left: `${p.x}%`, top: `${p.y}%` }}>
            <div className={cn("h-10 w-10 rounded-full bg-card shadow-md flex items-center justify-center ring-2", ring)}>
              {n.kind === "depot" ? <Boxes className="size-4 text-blue-600" /> : <Droplets className="size-4 text-green-600" />}
            </div>
            <span className="mt-1 rounded bg-black/80 text-white text-xs px-2 py-0.5 whitespace-nowrap">{n.name}{n.status !== "OPEN" ? ` · ${n.status}` : ""}</span>
          </div>
        );
      })}
      <div className="absolute left-3 bottom-3 rounded-lg bg-card/95 shadow p-3 text-xs space-y-1">
        <p className="font-semibold">Legend</p>
        <p className="flex items-center gap-2"><Boxes className="size-3 text-blue-600" />Depot ({depots.length})</p>
        <p className="flex items-center gap-2"><Droplets className="size-3 text-green-600" />Station ({stations.length})</p>
        <p className="flex items-center gap-2"><span className="w-4 border-t-2 border-blue-500" />Route available</p>
        <p className="flex items-center gap-2"><span className="w-4 border-t-2 border-dashed border-red-500" />Route disrupted</p>
      </div>
      <div className="absolute right-3 top-3 flex gap-1">{routes.filter((r) => r.status !== "AVAILABLE").map((r) => <Badge key={r.route_id} tone={statusTone(r.status)}>{r.route_id}</Badge>)}</div>
    </div>
  );
}
