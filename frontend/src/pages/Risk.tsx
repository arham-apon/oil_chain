import { useState } from "react";
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Flame, LineChart, Timer } from "lucide-react";
import { useApi } from "@/lib/api";
import { Badge, Card, CardTitle, Empty, PageHeader, Select, Table } from "@/components/ui";
import { cn, FUELS, pct } from "@/lib/utils";

const STATIONS = [["station-mirpur", "Mirpur"], ["station-tongi", "Tongi"], ["station-karnaphuli", "Karnaphuli"], ["station-coxsbazar", "Cox's Bazar"]];

export default function Risk() {
  const [sid, setSid] = useState("station-tongi");
  const [fuel, setFuel] = useState("DIESEL");
  const one = useApi("forecasts", `/api/forecasts?station_id=${sid}&fuel_type=${fuel}&history=64`, 4000);
  const all = useApi("forecasts", "/api/forecasts", 4000);
  const f = one.data?.forecasts?.[0];
  const data = [
    ...(one.data?.observed ?? []).map((o: any) => ({ tick: o.tick, observed: o.demand_liters, served: o.served_liters })),
    ...(f?.horizon ?? []).map((h: any) => ({ tick: h.tick, forecast: +h.mean.toFixed(1), band: [Math.max(0, h.mean - 2 * h.sigma), h.mean + 2 * h.sigma] })),
  ];
  const idx: Record<string, any> = {};
  (all.data?.forecasts ?? []).forEach((x: any) => (idx[`${x.station_id}/${x.fuel_type}`] = x));
  const heat = (r: number) => (r >= 0.5 ? "bg-red-500 text-white" : r >= 0.2 ? "bg-orange-400 text-white" : r >= 0.05 ? "bg-yellow-200 text-yellow-900" : "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300");

  return (
    <>
      <PageHeader title="Demand & Risk" sub="Observed demand vs ridge-regression forecast (±2σ band), stockout risk and time-to-empty">
        <Select value={sid} onChange={(e) => setSid(e.target.value)}>{STATIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>
        <Select value={fuel} onChange={(e) => setFuel(e.target.value)}>{FUELS.map((x) => <option key={x}>{x}</option>)}</Select>
      </PageHeader>
      {!one.data?.available && one.data && <Card className="border border-orange-300"><p className="text-sm">Forecast service unavailable ({one.data.error}). The decision engine is using the baseline demand formula.</p></Card>}
      <Card>
        <CardTitle icon={<LineChart className="text-blue-600" />} right={f && <div className="flex gap-2"><Badge tone={f.source === "MODEL" ? "green" : "purple"}>{f.source}</Badge><Badge>{f.model_version ?? "baseline"}</Badge></div>}>
          Demand forecast — {STATIONS.find((s) => s[0] === sid)?.[1]} {fuel}
        </CardTitle>
        <div className="h-80">
          <ResponsiveContainer>
            <ComposedChart data={data}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
              <XAxis dataKey="tick" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} unit=" L" /><Tooltip /><Legend />
              <Area dataKey="band" name="±2σ" stroke="none" fill="#3b82f6" fillOpacity={0.15} />
              <Line dataKey="observed" name="observed demand" stroke="#10b981" dot={false} strokeWidth={2} />
              <Line dataKey="served" name="served" stroke="#9ca3af" dot={false} strokeDasharray="4 3" />
              <Line dataKey="forecast" name="forecast" stroke="#3b82f6" dot={false} strokeWidth={2} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        {f && <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <Metric label="Burn rate" value={`${Math.round(f.burn_rate_lph)} L/h`} />
          <Metric label="Time to empty" value={f.t_empty_hours != null ? `${f.t_empty_hours} h (${f.t_empty_ticks} ticks)` : "> horizon"} />
          <Metric label="Stockout risk" value={pct(f.stockout_risk)} />
          <Metric label="Residual σ" value={`${f.residual_sigma.toFixed(1)} L/tick`} />
        </div>}
      </Card>
      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <CardTitle icon={<Flame className="text-red-500" />}>Stockout-risk heatmap</CardTitle>
          <div className="grid grid-cols-4 gap-1 text-sm">
            <div />{FUELS.map((x) => <div key={x} className="text-center text-xs font-medium">{x}</div>)}
            {STATIONS.map(([s, l]) => [<div key={s} className="font-medium text-sm self-center">{l}</div>, ...FUELS.map((x) => {
              const r = idx[`${s}/${x}`]?.stockout_risk ?? 0;
              return <button key={s + x} onClick={() => { setSid(s); setFuel(x); }} className={cn("rounded-md h-12 font-semibold", heat(r))}>{pct(r, 0)}</button>;
            })])}
          </div>
          <p className="text-xs text-muted-foreground">Click a cell to chart that pair.</p>
        </Card>
        <Card>
          <CardTitle icon={<Timer className="text-orange-500" />}>Time-to-empty</CardTitle>
          {(all.data?.forecasts ?? []).length === 0 && <Empty>No forecasts</Empty>}
          <Table head={["Station", "Fuel", "Inventory", "Burn L/h", "Empty in", "Risk"]}>
            {[...(all.data?.forecasts ?? [])].sort((a: any, b: any) => (a.t_empty_ticks ?? 999) - (b.t_empty_ticks ?? 999)).map((x: any) => (
              <tr key={x.station_id + x.fuel_type}><td>{STATIONS.find((s) => s[0] === x.station_id)?.[1]}</td><td>{x.fuel_type}</td>
                <td className="tabular">{Math.round(x.current_inventory ?? 0).toLocaleString()}</td><td className="tabular">{Math.round(x.burn_rate_lph)}</td>
                <td>{x.t_empty_hours != null ? `${x.t_empty_hours} h` : "> horizon"}</td><td><Badge tone={x.stockout_risk >= 0.5 ? "red" : x.stockout_risk >= 0.2 ? "amber" : "green"}>{pct(x.stockout_risk)}</Badge></td></tr>
            ))}
          </Table>
        </Card>
      </div>
    </>
  );
}

const Metric = ({ label, value }: { label: string; value: string }) => (
  <div className="rounded-lg bg-muted/50 p-3"><p className="text-xs text-muted-foreground">{label}</p><p className="font-semibold tabular">{value}</p></div>
);
