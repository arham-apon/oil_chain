import * as React from "react";
import { cn } from "@/lib/utils";

export const Card = ({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("bg-card text-card-foreground flex flex-col gap-4 rounded-xl border-0 shadow-sm p-5", className)} {...p} />
);

export const CardTitle = ({ icon, children, right }: { icon?: React.ReactNode; children: React.ReactNode; right?: React.ReactNode }) => (
  <div className="flex items-center justify-between gap-2">
    <h3 className="flex items-center gap-2 font-semibold [&_svg]:size-4">{icon}{children}</h3>
    {right}
  </div>
);

type BV = "default" | "outline" | "ghost" | "destructive";
export const Button = ({ className, variant = "outline", ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BV }) => (
  <button
    className={cn(
      "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium h-8 px-3 transition-all disabled:opacity-50 disabled:pointer-events-none [&_svg]:size-4",
      variant === "default" && "bg-blue-600 text-white hover:bg-blue-700",
      variant === "outline" && "border bg-background hover:bg-accent dark:bg-input/30",
      variant === "ghost" && "hover:bg-accent",
      variant === "destructive" && "bg-destructive text-white hover:bg-destructive/90",
      className,
    )}
    {...p}
  />
);

const tones: Record<string, string> = {
  green: "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-400",
  red: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400",
  amber: "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-400",
  blue: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-400",
  purple: "bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-400",
  gray: "bg-muted text-muted-foreground",
  dark: "bg-primary text-primary-foreground",
};
export const Badge = ({ tone = "gray", className, children }: { tone?: string; className?: string; children: React.ReactNode }) => (
  <span className={cn("inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium whitespace-nowrap", tones[tone], className)}>{children}</span>
);

const TONE: Record<string, string> = {
  OPEN: "green", AVAILABLE: "green", ARRIVED: "green", COMMITTED: "green", Healthy: "green", NORMAL: "green", CLOSED: "green", RUNNING: "green",
  IN_TRANSIT: "blue", SCHEDULED: "blue", AUTO_APPROVED: "blue", OPERATOR_APPROVED: "blue", COMMITTING: "blue", LOW: "blue",
  PENDING: "amber", STAGED_REVIEW: "amber", CONSTRAINED: "amber", DELAYED: "amber", Degraded: "amber", DEGRADED: "amber", HALF_OPEN: "amber", MEDIUM: "amber", PAUSED: "amber",
  FALLBACK: "purple",
  ACTIVE: "red", DISRUPTED: "red", OUTAGE: "red", FAILED: "red", SIM_REJECTED: "red", Down: "red", HIGH: "red", CRITICAL: "red",
};
export const statusTone = (s?: string | null) => TONE[s || ""] || "gray";
export const Status = ({ s }: { s?: string | null }) => <Badge tone={statusTone(s)}>{s ?? "—"}</Badge>;

export const Progress = ({ value, className, bar }: { value: number; className?: string; bar?: string }) => (
  <div className={cn("bg-primary/20 relative w-full overflow-hidden rounded-full h-1.5", className)}>
    <div className={cn("h-full transition-all", bar || "bg-primary")} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
  </div>
);

export const Tabs = ({ tabs, value, onChange }: { tabs: string[]; value: string; onChange: (v: string) => void }) => (
  <div className="inline-flex bg-muted rounded-lg p-1 gap-1">
    {tabs.map((t) => (
      <button key={t} onClick={() => onChange(t)}
        className={cn("px-3 h-7 rounded-md text-sm font-medium", value === t ? "bg-background shadow-sm" : "text-muted-foreground")}>{t}</button>
    ))}
  </div>
);

export const Table = ({ head, children }: { head: string[]; children: React.ReactNode }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-sm">
      <thead><tr className="border-b">{head.map((h) => <th key={h} className="text-left font-medium py-2 px-2 whitespace-nowrap">{h}</th>)}</tr></thead>
      <tbody className="[&_tr]:border-b [&_tr:last-child]:border-0 [&_td]:py-2 [&_td]:px-2 [&_tr:hover]:bg-muted/50">{children}</tbody>
    </table>
  </div>
);

export const PageHeader = ({ title, sub, children }: { title: string; sub: string; children?: React.ReactNode }) => (
  <div className="flex flex-wrap items-center justify-between gap-3">
    <div><h1 className="text-3xl font-bold">{title}</h1><p className="text-muted-foreground mt-1">{sub}</p></div>
    <div className="flex flex-wrap items-center gap-2">{children}</div>
  </div>
);

export const Input = (p: React.InputHTMLAttributes<HTMLInputElement>) => (
  <input {...p} className={cn("h-8 rounded-md px-3 text-sm bg-input-background border-0 outline-none focus:ring-2 focus:ring-ring/30", p.className)} />
);
export const Select = (p: React.SelectHTMLAttributes<HTMLSelectElement>) => (
  <select {...p} className={cn("h-8 rounded-md px-2 text-sm bg-input-background border-0", p.className)} />
);
export const Empty = ({ children }: { children: React.ReactNode }) => <p className="text-sm text-muted-foreground py-6 text-center">{children}</p>;

export function StatCard({ icon, color, value, label, sub, delta, deltaGood = true, progress }: {
  icon: React.ReactNode; color: string; value: React.ReactNode; label: string; sub: string; delta?: string; deltaGood?: boolean; progress?: number;
}) {
  return (
    <div className="bg-card rounded-xl shadow-sm hover:shadow-lg transition-all group p-3">
      <div className="flex items-center justify-between mb-2">
        <div className={cn("h-7 w-7 rounded-lg flex items-center justify-center text-white group-hover:scale-110 transition-transform [&_svg]:size-4", color)}>{icon}</div>
        {delta && <span className={cn("text-xs font-medium", deltaGood ? "text-green-600" : "text-red-600")}>{delta}</span>}
      </div>
      <h3 className="text-lg font-bold group-hover:text-blue-600 transition-colors tabular">{value}</h3>
      <p className="text-xs font-medium">{label}</p>
      <p className="text-xs text-muted-foreground">{sub}</p>
      {progress != null && <Progress className="mt-2 h-1" value={progress} />}
    </div>
  );
}

// Horizontal tank gauge: fill, in-flight (hatched) and a safety-stock marker.
export function Gauge({ label, inv, cap, inflight = 0, safety }: { label: string; inv: number; cap: number; inflight?: number; safety?: number | null }) {
  const f = (inv / cap) * 100, fi = (inflight / cap) * 100;
  const bar = f < 20 ? "bg-red-500" : f < 40 ? "bg-orange-500" : "bg-blue-600";
  return (
    <div>
      <div className="flex justify-between text-xs mb-1"><span className="font-medium">{label}</span>
        <span className="text-muted-foreground tabular">{Math.round(inv).toLocaleString()} / {Math.round(cap).toLocaleString()} L</span></div>
      <div className="relative h-2.5 w-full rounded-full bg-muted overflow-hidden">
        <div className={cn("absolute inset-y-0 left-0", bar)} style={{ width: `${Math.min(100, f)}%` }} />
        <div className="absolute inset-y-0 bg-blue-300/70 dark:bg-blue-400/40" style={{ left: `${Math.min(100, f)}%`, width: `${Math.min(100 - f, fi)}%` }} />
        {safety != null && <div className="absolute inset-y-0 w-0.5 bg-red-600" style={{ left: `${Math.min(100, (safety / cap) * 100)}%` }} title="safety stock" />}
      </div>
      <div className="flex justify-between text-[11px] text-muted-foreground mt-0.5">
        <span>{f.toFixed(0)}% full{inflight > 0 ? ` · +${Math.round(inflight).toLocaleString()} L in flight` : ""}</span>
        {safety != null && <span>safety {Math.round(safety).toLocaleString()} L</span>}
      </div>
    </div>
  );
}
