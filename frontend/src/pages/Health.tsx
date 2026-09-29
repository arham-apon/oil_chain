import { useState } from "react";
import { Activity, CircleGauge, ExternalLink, FlaskConical, Pause, Play, RotateCcw, SkipForward, Zap } from "lucide-react";
import { post, useApi } from "@/lib/api";
import { Badge, Button, Card, CardTitle, Input, PageHeader, Select, StatCard, Status } from "@/components/ui";
import { cn } from "@/lib/utils";

export default function Health() {
  const h = useApi("health", "/api/health/components", 3000);
  const ov = useApi("overview", "/api/overview", 2000);
  const d = h.data, m = d?.metrics ?? {};
  const ds = ov.data?.mode_detail;
  return (
    <>
      <PageHeader title="System Health" sub="Component status, circuit breakers, latency and fallbacks">
        <Badge tone={d?.overall === "Healthy" ? "green" : d?.overall === "Degraded" ? "amber" : "red"}>{d?.overall ?? "…"}</Badge>
        <Button onClick={() => window.open(d?.grafana_url ?? "http://localhost:3001", "_blank")}><ExternalLink />Grafana</Button>
      </PageHeader>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard icon={<CircleGauge />} color="bg-blue-500" value={m.p95_latency_ms != null ? `${m.p95_latency_ms} ms` : "n/a"} label="p95 latency" sub="all services, 2 min" />
        <StatCard icon={<Activity />} color="bg-red-500" value={m.error_rate_pct != null ? `${m.error_rate_pct}%` : "n/a"} label="Error rate" sub="HTTP 5xx, 2 min" />
        <StatCard icon={<Zap />} color="bg-green-500" value={m.rps ?? "n/a"} label="Throughput" sub="requests / s" />
        <StatCard icon={<RotateCcw />} color="bg-purple-500" value={m.fallback_activations ?? "n/a"} label="Fallback activations" sub="total since start" />
        <StatCard icon={<CircleGauge />} color="bg-orange-500" value={m.sim_client_p95_ms != null ? `${m.sim_client_p95_ms} ms` : "n/a"} label="Simulator p95" sub="client latency" />
      </div>
      {!m.available && d && <p className="text-xs text-muted-foreground">Prometheus not reachable — latency figures unavailable.</p>}
      <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
        {(d?.components ?? []).map((c: any) => (
          <Card key={c.name} className="gap-2">
            <div className="flex items-center gap-2">
              <span className={cn("h-2.5 w-2.5 rounded-full", c.status === "Healthy" ? "bg-green-500" : c.status === "Degraded" ? "bg-orange-500" : "bg-red-500")} />
              <span className="font-semibold">{c.name}</span><span className="ml-auto"><Status s={c.status} /></span>
            </div>
            <p className="text-sm text-muted-foreground">{c.detail}</p>
            <div className="flex gap-2 text-xs">{c.latency_ms != null && <Badge>{c.latency_ms} ms</Badge>}{c.breaker && <Badge tone={c.breaker === "CLOSED" ? "green" : "red"}>breaker {c.breaker}</Badge>}</div>
          </Card>
        ))}
      </div>
      {ds?.breakers && <Card><CardTitle>Decision engine</CardTitle>
        <div className="flex flex-wrap gap-2 text-sm"><Badge tone={ov.data?.mode === "NORMAL" ? "green" : "purple"}>mode {ov.data?.mode}</Badge>
          {Object.entries<any>(ds.breakers).map(([k, v]) => <Badge key={k} tone={v === "CLOSED" ? "green" : "red"}>{k}: {v}</Badge>)}
          {ds.last_cycle && <Badge>last cycle {ds.last_cycle.duration_ms} ms · {ds.last_cycle.planner} · {ds.last_cycle.proposals} proposals</Badge>}</div></Card>}
      {ov.data?.demo_controls && <DemoControls />}
    </>
  );
}

function DemoControls() {
  const [msg, setMsg] = useState("");
  const [ev, setEv] = useState({ type: "demand_spike", duration_ticks: "24", target: "station-tongi", multiplier: "1.8" });
  const [fault, setFault] = useState({ type: "error_rate", duration_seconds: "60" });
  const run = (p: string, b?: any) => post(p, b).then(() => setMsg(`OK: ${p}`)).catch((e) => setMsg(`Error: ${e.message}`));
  const eventBody = () => {
    const t = ev.target, params: any = {};
    if (ev.type === "demand_spike") { params.multiplier = +ev.multiplier; params[t.startsWith("region") ? "region_ids" : "station_ids"] = [t]; }
    else if (ev.type === "route_disruption") params.route_ids = [t];
    else if (ev.type === "station_outage") params.station_ids = [t];
    else { params.depot_ids = [t]; if (ev.type === "shipment_delay") params.delay_ticks = 2; if (ev.type === "supply_shortfall") params.factor = 0.5; }
    return { type: ev.type, duration_ticks: +ev.duration_ticks, parameters: params };
  };
  return (
    <Card>
      <CardTitle icon={<FlaskConical className="text-orange-500" />} right={<Badge tone="amber">DEMO_CONTROLS — simulator /admin, audited</Badge>}>Simulation Controls</CardTitle>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => run("/api/demo/run")}><Play />Run</Button><Button onClick={() => run("/api/demo/pause")}><Pause />Pause</Button>
        <Button onClick={() => run("/api/demo/step")}><SkipForward />Step</Button>
        <Button variant="destructive" onClick={() => confirm("Reset the simulator to tick 0?") && run("/api/demo/reset")}><RotateCcw />Reset</Button>
      </div>
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-sm font-medium w-16">Event</span>
        <Select value={ev.type} onChange={(e) => setEv({ ...ev, type: e.target.value })}>{["demand_spike", "route_disruption", "station_outage", "depot_constraint", "shipment_delay", "supply_shortfall"].map((x) => <option key={x}>{x}</option>)}</Select>
        <Input value={ev.target} onChange={(e) => setEv({ ...ev, target: e.target.value })} placeholder="target id" className="w-52" />
        {ev.type === "demand_spike" && <Input value={ev.multiplier} onChange={(e) => setEv({ ...ev, multiplier: e.target.value })} className="w-16" />}
        <Input value={ev.duration_ticks} onChange={(e) => setEv({ ...ev, duration_ticks: e.target.value })} className="w-16" title="duration ticks" />
        <Button onClick={() => run("/api/demo/events", eventBody())}>Inject (starts now+2)</Button>
      </div>
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-sm font-medium w-16">Fault</span>
        <Select value={fault.type} onChange={(e) => setFault({ ...fault, type: e.target.value })}>{["latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"].map((x) => <option key={x}>{x}</option>)}</Select>
        <Input value={fault.duration_seconds} onChange={(e) => setFault({ ...fault, duration_seconds: e.target.value })} className="w-16" title="seconds" />
        <Button onClick={() => run("/api/demo/faults", { type: fault.type, duration_seconds: +fault.duration_seconds, parameters: fault.type === "latency" ? { delay_ms: 500 } : fault.type === "error_rate" ? { rate: 0.25 } : {} })}>Inject</Button>
        <Button onClick={() => run("/api/demo/faults/clear")}>Clear faults</Button>
      </div>
      {msg && <p className="text-sm text-muted-foreground">{msg}</p>}
    </Card>
  );
}
