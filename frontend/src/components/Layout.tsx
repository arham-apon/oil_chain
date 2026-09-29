import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Activity, AlertTriangle, Bell, Bot, ChevronLeft, ClipboardList, Droplets, ExternalLink, Fuel, Gauge, LayoutDashboard,
  Menu, Moon, PlayCircle, Search, ShieldCheck, Sun, Truck, TrendingUp,
} from "lucide-react";
import { api, operator, post, useApi } from "@/lib/api";
import { Badge, statusTone } from "@/components/ui";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  { to: "/inventory", label: "Inventory", icon: Droplets },
  { to: "/risk", label: "Demand & Risk", icon: TrendingUp },
  { to: "/supply", label: "Supply & Disruptions", icon: Truck },
  { to: "/decisions", label: "Decision Center", icon: ClipboardList, badge: "staged" },
  { to: "/copilot", label: "Copilot", icon: Bot },
  { to: "/health", label: "System Health", icon: Activity },
  { to: "/audit", label: "Audit", icon: ShieldCheck },
];
const SEARCH = [
  ["station-mirpur", "Mirpur station", "/inventory"], ["station-tongi", "Tongi station", "/inventory"],
  ["station-karnaphuli", "Karnaphuli station", "/inventory"], ["station-coxsbazar", "Cox's Bazar station", "/inventory"],
  ["depot-gazipur", "Gazipur depot", "/inventory"], ["depot-patiya", "Patiya depot", "/inventory"],
  ["routes", "Route status", "/supply"], ["events", "Events & incidents", "/supply"], ["decisions", "Staged decisions", "/decisions"],
  ["forecast", "Forecasts & stockout risk", "/risk"], ["health", "Component health", "/health"], ["audit", "Audit log", "/audit"],
];

export default function Layout() {
  const [open, setOpen] = useState(true);
  const [dark, setDark] = useState(() => localStorage.getItem("theme") === "dark");
  const [q, setQ] = useState("");
  const [bell, setBell] = useState(false);
  const [op, setOp] = useState(operator());
  const nav = useNavigate();
  const ov = useApi("overview", "/api/overview", 2000);
  const alerts = useApi<any[]>("alerts", "/api/alerts?open_only=true&limit=15", 5000);
  const health = useApi("health", "/api/health/components", 5000);
  const o = ov.data;
  useEffect(() => { document.documentElement.classList.toggle("dark", dark); localStorage.setItem("theme", dark ? "dark" : "light"); }, [dark]);
  const hits = q ? SEARCH.filter((s) => (s[0] + s[1]).toLowerCase().includes(q.toLowerCase())) : [];
  const stale = o?.simulator?.stale;
  const mode = o?.mode ?? (ov.isError ? "DEGRADED" : "…");

  return (
    <div className="h-screen flex flex-col bg-page">
      <header className="h-16 bg-card border-b flex items-center justify-between px-6 shadow-sm shrink-0 gap-4">
        <div className="flex items-center gap-4">
          <button className="h-8 px-2 rounded-md hover:bg-accent" onClick={() => setOpen(!open)}><Menu className="size-4" /></button>
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 bg-gradient-to-br from-blue-600 to-blue-700 rounded-lg flex items-center justify-center shadow-sm"><Fuel className="size-4 text-white" /></div>
            <div className="hidden sm:block"><h1 className="font-semibold leading-tight">FuelOps</h1><p className="text-xs text-muted-foreground">Supply Operations Platform</p></div>
          </div>
        </div>
        <div className="flex-1 max-w-xl mx-4 relative hidden md:block">
          <Search className="size-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search stations, depots, routes, decisions..."
            className="w-full pl-10 pr-4 h-10 rounded-md bg-muted/50 focus:bg-background focus:ring-2 focus:ring-ring/20 outline-none text-sm" />
          {hits.length > 0 && (
            <div className="absolute z-50 mt-1 w-full bg-popover border rounded-md shadow-lg p-1">
              {hits.map((h) => <button key={h[0]} onClick={() => { nav(h[2]); setQ(""); }} className="w-full text-left px-3 py-2 text-sm rounded hover:bg-accent">{h[1]} <span className="text-xs text-muted-foreground">{h[0]}</span></button>)}
            </div>
          )}
        </div>
        <div className="flex items-center gap-3">
          <div className="hidden lg:flex flex-col items-end text-xs">
            <span className="font-medium tabular">Tick {o?.tick?.tick ?? "—"}</span>
            <span className="text-muted-foreground">{o?.tick?.sim_time ? new Date(o.tick.sim_time).toISOString().slice(0, 16).replace("T", " ") : "—"}</span>
          </div>
          <Badge tone={statusTone(o?.tick?.status)}>{o?.tick?.status ?? "…"}</Badge>
          <Badge tone={statusTone(mode)}>{mode}</Badge>
          <Badge tone="purple" className="hidden xl:inline-flex">SIMULATED DATA</Badge>
          <div className="flex items-center gap-1 bg-muted/50 rounded-lg p-1">
            <button onClick={() => setDark(false)} className={cn("h-7 w-7 rounded-md flex items-center justify-center", !dark && "bg-background shadow-sm")}><Sun className="size-4" /></button>
            <button onClick={() => setDark(true)} className={cn("h-7 w-7 rounded-md flex items-center justify-center", dark && "bg-background shadow-sm")}><Moon className="size-4" /></button>
          </div>
          <div className="relative">
            <button onClick={() => setBell(!bell)} className="h-8 w-8 rounded-md hover:bg-accent flex items-center justify-center relative">
              <Bell className="size-4" />
              {(o?.counts?.alerts_open ?? 0) > 0 && <span className="absolute -top-1 -right-1 h-5 min-w-5 px-1 rounded-full bg-destructive text-white text-xs flex items-center justify-center">{o.counts.alerts_open}</span>}
            </button>
            {bell && (
              <div className="absolute right-0 z-50 mt-2 w-96 bg-popover border rounded-lg shadow-lg p-2 max-h-96 overflow-auto">
                <div className="flex justify-between items-center px-2 py-1"><span className="font-semibold text-sm">Alerts</span>
                  <button className="text-xs text-blue-600" onClick={() => post("/api/alerts/ack-all").then(() => { alerts.refetch(); ov.refetch(); })}>Acknowledge all</button></div>
                {(alerts.data ?? []).length === 0 && <p className="text-sm text-muted-foreground p-3">No open alerts</p>}
                {(alerts.data ?? []).map((a) => (
                  <div key={a.id} className="px-2 py-2 border-b last:border-0 text-sm">
                    <div className="flex items-center gap-2"><Badge tone={statusTone(a.severity)}>{a.severity}</Badge><span className="font-medium">{a.kind}</span><span className="ml-auto text-xs text-muted-foreground">t{a.tick}</span></div>
                    <p className="text-xs text-muted-foreground mt-1">{a.message}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className="h-8 w-8 rounded-full bg-muted flex items-center justify-center text-xs font-semibold uppercase">{op.slice(0, 2)}</div>
            <div className="hidden md:block">
              <input value={op} onChange={(e) => { setOp(e.target.value); localStorage.setItem("operator", e.target.value || "operator"); }}
                className="text-sm font-medium bg-transparent outline-none w-24" title="Operator name (recorded in the audit log)" />
              <p className="text-xs text-muted-foreground">Operator</p>
            </div>
          </div>
        </div>
      </header>

      {stale && <div className="bg-orange-500 text-white text-sm px-6 py-1.5 flex items-center gap-2"><AlertTriangle className="size-4" />STALE DATA — the simulator is serving stale data. Autonomous dispatch is suspended; every proposal needs operator review.</div>}
      {ov.isError && <div className="bg-red-600 text-white text-sm px-6 py-1.5">Gateway unreachable — showing last known data.</div>}

      <div className="flex flex-1 min-h-0">
        <aside className={cn("bg-sidebar border-r border-sidebar-border flex flex-col transition-all duration-300 shrink-0", open ? "w-72" : "w-0 overflow-hidden")}>
          <div className="p-4 border-b border-sidebar-border flex items-center justify-between">
            <div><h2 className="font-semibold">Navigation</h2><p className="text-xs text-sidebar-foreground/60">Monitor and act on the fuel network</p></div>
            <button onClick={() => setOpen(false)} className="h-8 px-2 rounded-md hover:bg-sidebar-accent"><ChevronLeft className="size-4" /></button>
          </div>
          <nav className="flex-1 p-2 space-y-1 overflow-y-auto">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end className={({ isActive }) => cn("flex items-center gap-2 h-11 px-3 rounded-md text-sm font-medium transition-all duration-200",
                isActive ? "bg-primary text-primary-foreground shadow-md" : "hover:bg-accent hover:translate-x-1")}>
                <n.icon className="size-4" /><span className="flex-1">{n.label}</span>
                {n.badge === "staged" && (o?.counts?.staged ?? 0) > 0 && <span className="h-5 min-w-5 px-1 rounded-md bg-destructive text-white text-xs flex items-center justify-center">{o.counts.staged}</span>}
                {n.to === "/supply" && (o?.counts?.active_events ?? 0) > 0 && <Badge tone="red">{o.counts.active_events} active</Badge>}
              </NavLink>
            ))}
            <div className="pt-4 mt-2 border-t border-sidebar-border">
              <p className="px-3 pb-2 text-xs font-semibold text-sidebar-foreground/60">QUICK ACTIONS</p>
              <QuickAction icon={<PlayCircle className="text-orange-500" />} label="Run decision cycle" onClick={() => post("/api/cycle/run?dry_run=false")} />
              <QuickAction icon={<Gauge className="text-blue-500" />} label="Simulate what-if" onClick={() => nav("/decisions")} />
              <QuickAction icon={<AlertTriangle className="text-red-500" />} label="Check alerts" onClick={() => setBell(true)} />
              <QuickAction icon={<ExternalLink className="text-green-500" />} label="Open Grafana" onClick={() => window.open(o?.grafana_url ?? "http://localhost:3001", "_blank")} />
            </div>
          </nav>
          <div className="p-4">
            <div className="rounded-lg bg-sidebar-accent p-3 text-xs">
              <div className="flex items-center gap-2 font-medium mb-1">
                <span className={cn("h-2 w-2 rounded-full", health.data?.overall === "Healthy" ? "bg-green-500" : health.data?.overall === "Degraded" ? "bg-orange-500" : "bg-red-500")} />System Status</div>
              <p className="text-sidebar-foreground/70">{health.data ? `${health.data.overall} · ${health.data.components.filter((c: any) => c.status !== "Healthy").length} components need attention` : "checking…"}</p>
              <p className="text-sidebar-foreground/50 mt-1">SSE {o?.simulator?.sse_connected ? "connected" : "polling"} · sim {o?.simulator?.state ?? "?"}</p>
            </div>
          </div>
        </aside>
        <main className="flex-1 overflow-y-auto p-6 space-y-6 min-w-0"><Outlet /></main>
      </div>
    </div>
  );
}

function QuickAction({ icon, label, onClick }: { icon: React.ReactNode; label: string; onClick: () => void }) {
  return <button onClick={onClick} className="w-full flex items-center gap-2 px-3 h-9 rounded-md text-sm hover:bg-accent [&_svg]:size-4">{icon}{label}</button>;
}

export { api };
