import { useEffect, useState } from "react";
import { Brain, Check, ClipboardList, FlaskConical, Pencil, Plus, Sparkles, X } from "lucide-react";
import { post, useApi } from "@/lib/api";
import { Badge, Button, Card, CardTitle, Empty, Input, PageHeader, Select, Status, Table, Tabs } from "@/components/ui";
import { cn, FUELS, fmtL, pct } from "@/lib/utils";

const ROUTES: Record<string, string[]> = {
  "station-mirpur": ["route-gazipur-mirpur", "route-patiya-mirpur"], "station-tongi": ["route-gazipur-tongi"],
  "station-karnaphuli": ["route-patiya-karnaphuli", "route-gazipur-karnaphuli"], "station-coxsbazar": ["route-patiya-coxsbazar"],
};

export default function Decisions() {
  const [tab, setTab] = useState("Staged");
  const q = tab === "Staged" ? "?status=STAGED_REVIEW" : "?limit=200";
  const list = useApi<any[]>("decisions", `/api/decisions${q}`, 3000);
  const [sel, setSel] = useState<string | null>(null);
  const [manual, setManual] = useState(false);
  const rows = list.data ?? [];
  const d = rows.find((r) => r.decision_id === sel) ?? rows[0];

  return (
    <>
      <PageHeader title="Decision Center" sub="Optimizer proposals, System-1 triage, grounded explanations — approve, edit or reject">
        <Tabs tabs={["Staged", "History"]} value={tab} onChange={setTab} />
        <Button onClick={() => setManual(!manual)}><Plus />Manual allocation</Button>
      </PageHeader>
      {manual && <ManualForm onDone={() => { setManual(false); setTab("Staged"); list.refetch(); }} />}
      <div className="grid xl:grid-cols-[1fr_440px] gap-6 items-start">
        <Card>
          <CardTitle icon={<ClipboardList className="text-blue-600" />}>{tab === "Staged" ? `Awaiting review (${rows.length})` : "All decisions"}</CardTitle>
          {rows.length === 0 && <Empty>{tab === "Staged" ? "Nothing waiting for review — auto-approved dispatches continue in the background." : "No decisions yet"}</Empty>}
          {rows.length > 0 && (
            <Table head={["Tick", "Station", "Fuel", "Route", "Qty", "Urgency", "Origin", "Status", ""]}>
              {rows.map((r) => (
                <tr key={r.decision_id} onClick={() => setSel(r.decision_id)} className={cn("cursor-pointer", d?.decision_id === r.decision_id && "bg-blue-50 dark:bg-blue-950/30")}>
                  <td className="tabular">{r.cycle_tick}</td><td className="font-medium">{r.station_id?.replace("station-", "")}</td><td>{r.fuel_type}</td>
                  <td className="text-xs">{r.route_id?.replace("route-", "")}</td><td className="tabular">{fmtL(r.quantity)}</td>
                  <td><Urgency j={r.jev} /></td><td><Badge tone={r.system_origin === "SYSTEM1_AUTO" ? "blue" : r.system_origin === "HEURISTIC_FALLBACK" ? "purple" : "amber"}>{r.system_origin}</Badge></td>
                  <td><Status s={r.status} /></td><td><Button className="h-7">View</Button></td></tr>
              ))}
            </Table>
          )}
        </Card>
        {d && <Detail key={d.decision_id} d={d} refresh={() => list.refetch()} />}
      </div>
    </>
  );
}

const Urgency = ({ j }: { j: any }) => {
  const u = j?.urgency_1to5 ?? j?.deterministic_urgency;
  if (u == null) return <span className="text-muted-foreground">—</span>;
  return <Badge tone={u >= 4.5 ? "red" : u >= 3 ? "amber" : "green"}>{Number(u).toFixed(1)}</Badge>;
};

function Detail({ d, refresh }: { d: any; refresh: () => void }) {
  const f = d.facts || {}, j = d.jev || {}, n = d.explanation?.narrative;
  const [qty, setQty] = useState(String(d.quantity));
  const [route, setRoute] = useState(d.route_id);
  const [note, setNote] = useState("");
  const [edit, setEdit] = useState(false);
  const [sim, setSim] = useState<any>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const staged = d.status === "STAGED_REVIEW";
  useEffect(() => setSim(null), [qty, route]);

  const act = async (fn: () => Promise<any>, ok: string) => {
    setBusy(true); setMsg(null);
    try { const r = await fn(); setMsg({ ok: true, text: `${ok}${r?.result ? ` → ${r.result}` : ""}${r?.sim_allocation_id ? ` (allocation #${r.sim_allocation_id})` : ""}` }); refresh(); }
    catch (e: any) { setMsg({ ok: false, text: e.message }); } finally { setBusy(false); }
  };
  const simulate = () => act(async () => setSim(await post("/api/whatif", { proposals: [{ station_id: d.station_id, fuel_type: d.fuel_type, qty: +qty, route_id: route }] })), "Simulated (nothing dispatched)");

  const rows: [string, React.ReactNode][] = [
    ["Station / fuel", `${f.alert_entity_id} · ${f.fuel_type}`], ["Current inventory", fmtL(f.current_inventory_liters)],
    ["Tank capacity", fmtL(f.tank_capacity_liters)], ["In flight", fmtL(f.in_flight_liters)],
    ["Burn rate", `${f.burn_rate_liters_per_hour ?? "—"} L/h`], ["Projected stockout", f.projected_stockout_hours != null ? `${f.projected_stockout_hours} h` : "beyond horizon"],
    ["Expected demand", `${fmtL(f.expected_demand_liters)} / ${f.horizon_ticks} ticks`], ["Recommended", fmtL(f.recommended_allocation_liters)],
    ["Source depot / route", `${f.source_depot_id} · ${f.transit_route_id} (${f.transit_ticks} ticks)`],
    ["Stockout risk", <span key="r"><b className="text-red-600">{pct(f.stockout_risk_before)}</b> → <b className="text-green-600">{pct(f.stockout_risk_after)}</b></span>],
    ["Forecast", `${f.confidence?.forecast_source ?? "—"} σ=${f.confidence?.forecast_sigma ?? "—"}`], ["Gate", f.gate_reason ?? "—"],
  ];

  return (
    <Card className="xl:sticky xl:top-0">
      <CardTitle right={<Status s={d.status} />}>Decision Details</CardTitle>
      <p className="text-xs text-muted-foreground -mt-2 font-mono">{d.idempotency_key}</p>
      <div className="text-sm space-y-1.5">{rows.map(([k, v]) => <div key={k} className="flex justify-between gap-3"><span className="text-muted-foreground">{k}</span><span className="font-medium text-right">{v}</span></div>)}</div>
      {f.binding_constraints?.length > 0 && <div className="flex flex-wrap gap-1">{f.binding_constraints.map((b: string) => <Badge key={b} tone="amber">{b}</Badge>)}</div>}

      <div className="rounded-lg bg-muted/50 p-3 space-y-2">
        <p className="font-semibold text-sm flex items-center gap-2"><Brain className="size-4 text-purple-600" />System 1 triage (Jev) <Badge className="ml-auto">{j.source ?? "—"}</Badge></p>
        <div className="grid grid-cols-3 gap-2 text-xs">
          <div><p className="text-muted-foreground">Urgency 1–5</p><p className="font-semibold">{j.urgency_1to5?.toFixed?.(2) ?? j.deterministic_urgency ?? "—"}{j.urgency_conf != null && ` (${pct(j.urgency_conf, 0)})`}</p></div>
          <div><p className="text-muted-foreground">Crisis class</p><p className="font-semibold">{j.crisis_class ?? "—"}</p></div>
          <div><p className="text-muted-foreground">Auto-approve</p><p className="font-semibold">{j.auto_approve != null ? pct(j.auto_approve, 0) : "—"}</p></div>
        </div>
      </div>

      <div className="rounded-lg border p-3 space-y-2 text-sm">
        <p className="font-semibold flex items-center gap-2"><Sparkles className="size-4 text-blue-600" />Explanation <Badge className="ml-auto">{d.explanation?.source ?? "pending"}</Badge></p>
        {!n && <Button className="h-7" onClick={() => act(() => post(`/api/decisions/${d.decision_id}/explain`), "Explanation generated")}>Generate</Button>}
        {n && <>
          <p>{n.summary}</p>
          <ul className="list-disc pl-4 text-xs text-muted-foreground">{n.primary_causal_factors?.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>
          <p className="text-xs"><b>Risk:</b> {n.risk_mitigation_delta}</p>
          <p className="text-xs"><b>Contingency:</b> {n.fallback_contingency}</p>
          {n.operator_checks?.length > 0 && <p className="text-xs"><b>Check:</b> {n.operator_checks.join(" · ")}</p>}
        </>}
      </div>

      {f.alternatives?.length > 0 && <div className="text-xs space-y-1"><p className="font-semibold text-sm">Alternatives</p>
        {f.alternatives.map((a: any, i: number) => <p key={i} className="text-muted-foreground">{a.route_id ? `${a.route_id}: ${a.validator}${a.feasible_qty ? `, ${fmtL(a.feasible_qty)}, risk after ${pct(a.stockout_risk_after)}` : ""}` : a.note}</p>)}</div>}

      {staged && <div className="space-y-2 border-t pt-3">
        {edit && <div className="grid grid-cols-2 gap-2">
          <Input type="number" value={qty} onChange={(e) => setQty(e.target.value)} min={1} step={10} />
          <Select value={route} onChange={(e) => setRoute(e.target.value)}>{(ROUTES[d.station_id] ?? [d.route_id]).map((r) => <option key={r}>{r}</option>)}</Select>
          <Input className="col-span-2" placeholder="Operator note (audited)" value={note} onChange={(e) => setNote(e.target.value)} />
        </div>}
        <div className="flex flex-wrap gap-2">
          <Button onClick={simulate} disabled={busy}><FlaskConical />Simulate</Button>
          <Button variant="default" disabled={busy} onClick={() => act(() => post(`/api/decisions/${d.decision_id}/approve`, edit ? { qty: +qty, route_id: route, note } : { note }), edit ? "Approved with edits" : "Approved")}><Check />{edit ? "Approve edited" : "Approve"}</Button>
          <Button onClick={() => setEdit(!edit)}><Pencil />{edit ? "Cancel edit" : "Edit"}</Button>
          <Button variant="destructive" disabled={busy} onClick={() => act(() => post(`/api/decisions/${d.decision_id}/reject`, { note }), "Rejected")}><X />Reject</Button>
        </div>
      </div>}
      {d.sim_allocation_id && <p className="text-sm">Simulator allocation <b>#{d.sim_allocation_id}</b> — track it in Audit → Allocation ledger.</p>}
      {d.sim_error_code && <Badge tone="red">{d.sim_error_code}</Badge>}
      {msg && <p className={cn("text-sm", msg.ok ? "text-green-600" : "text-red-600")}>{msg.text}</p>}
      {sim && <WhatIf r={sim} />}
    </Card>
  );
}

export function WhatIf({ r }: { r: any }) {
  return (
    <div className="rounded-lg bg-blue-50 dark:bg-blue-950/30 p-3 text-xs space-y-1">
      <p className="font-semibold text-sm">What-if result <span className="font-normal text-muted-foreground">(simulation only)</span></p>
      {r.proposals?.map((p: any, i: number) => <p key={i}>Pre-flight validator: <Badge tone={p.validator === "OK" ? "green" : "red"}>{p.validator}</Badge></p>)}
      {r.pairs?.map((p: any) => <p key={p.station_id + p.fuel_type}>{p.station_id} {p.fuel_type}: risk {pct(p.stockout_risk_before)} → <b>{pct(p.stockout_risk_after)}</b>, empty in {p.t_empty_before ?? "∞"} → {p.t_empty_after ?? "∞"} ticks</p>)}
      {Object.entries<any>(r.depots ?? {}).map(([k, v]) => <p key={k}>{k}: {fmtL(v.remaining)} remaining after</p>)}
    </div>
  );
}

function ManualForm({ onDone }: { onDone: () => void }) {
  const [s, setS] = useState("station-mirpur");
  const [fuel, setFuel] = useState("DIESEL");
  const [route, setRoute] = useState(ROUTES["station-mirpur"][0]);
  const [qty, setQty] = useState("3000");
  const [note, setNote] = useState("");
  const [sim, setSim] = useState<any>(null);
  const [err, setErr] = useState("");
  const body = { station_id: s, fuel_type: fuel, qty: +qty, route_id: route };
  return (
    <Card>
      <CardTitle icon={<Plus className="text-blue-600" />}>Manual allocation (staged for approval)</CardTitle>
      <div className="flex flex-wrap gap-2">
        <Select value={s} onChange={(e) => { setS(e.target.value); setRoute(ROUTES[e.target.value][0]); }}>{Object.keys(ROUTES).map((x) => <option key={x}>{x}</option>)}</Select>
        <Select value={fuel} onChange={(e) => setFuel(e.target.value)}>{FUELS.map((x) => <option key={x}>{x}</option>)}</Select>
        <Select value={route} onChange={(e) => setRoute(e.target.value)}>{ROUTES[s].map((x) => <option key={x}>{x}</option>)}</Select>
        <Input type="number" value={qty} onChange={(e) => setQty(e.target.value)} className="w-28" />
        <Input placeholder="note" value={note} onChange={(e) => setNote(e.target.value)} />
        <Button onClick={() => post("/api/whatif", { proposals: [body] }).then(setSim).catch((e) => setErr(e.message))}><FlaskConical />Simulate</Button>
        <Button variant="default" onClick={() => post("/api/decisions/manual", { ...body, note, staged: true }).then(onDone).catch((e) => setErr(e.message))}>Create</Button>
      </div>
      {err && <p className="text-sm text-red-600">{err}</p>}
      {sim && <WhatIf r={sim} />}
    </Card>
  );
}
