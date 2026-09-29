import { QueryClient, useQuery } from "@tanstack/react-query";

export const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 1000 } } });
export const operator = () => localStorage.getItem("operator") || "operator";

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-Operator": operator(), ...(init?.headers || {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? body));
  return body as T;
}
export const post = <T = any>(p: string, b?: unknown) => api<T>(p, { method: "POST", body: JSON.stringify(b ?? {}) });

// REST is the source of truth; polling keeps the UI alive when the WebSocket is down.
export const useApi = <T = any>(key: string, path: string, ms = 3000) =>
  useQuery<T>({ queryKey: [key, path], queryFn: () => api<T>(path), refetchInterval: ms });

const INVALIDATE: Record<string, string[]> = {
  "state.updated": ["overview", "stations", "depots", "routes", "metrics"],
  "decisions.updated": ["decisions", "overview"],
  alert: ["alerts", "overview"],
  "event.changed": ["events", "routes", "incidents"],
  "allocation.changed": ["allocations"],
  "sim.fault": ["overview", "health"],
  health: ["health"],
};

export function connectWs() {
  let ws: WebSocket | null = null;
  let stop = false;
  const open = () => {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onmessage = (m) => {
      const t = JSON.parse(m.data).type as string;
      (INVALIDATE[t] || []).forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
    };
    ws.onclose = () => { if (!stop) setTimeout(open, 2000); };
  };
  open();
  return () => { stop = true; ws?.close(); };
}
