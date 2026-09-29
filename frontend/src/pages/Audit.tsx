import { useState } from "react";
import { FileText, Package } from "lucide-react";
import { useApi } from "@/lib/api";
import { Badge, Card, CardTitle, Empty, Input, PageHeader, Status, Table, Tabs } from "@/components/ui";
import { fmtL } from "@/lib/utils";

export default function Audit() {
  const [tab, setTab] = useState("Audit log");
  const [filter, setFilter] = useState("");
  const audit = useApi<any[]>("audit", `/api/audit?limit=300${filter ? `&action=${encodeURIComponent(filter)}` : ""}`, 5000);
  const alloc = useApi<any[]>("allocations", "/api/allocations?limit=300", 4000);
  return (
    <>
      <PageHeader title="Audit" sub="Every dispatch, rejection, cancel, fallback, operator action and demo control is recorded">
        <Tabs tabs={["Audit log", "Allocation ledger"]} value={tab} onChange={setTab} />
      </PageHeader>
      {tab === "Audit log" ? (
        <Card>
          <CardTitle icon={<FileText className="text-blue-600" />} right={<Input placeholder="filter action prefix, e.g. decision." value={filter} onChange={(e) => setFilter(e.target.value)} className="w-64" />}>Audit log</CardTitle>
          {(audit.data ?? []).length === 0 && <Empty>No audit entries</Empty>}
          <Table head={["Time", "Tick", "Actor", "Action", "Entity", "Result", "Data"]}>
            {(audit.data ?? []).map((a) => (
              <tr key={a.id}><td className="text-xs whitespace-nowrap">{new Date(a.at).toLocaleTimeString()}</td><td className="tabular">{a.tick ?? "—"}</td>
                <td>{a.actor}</td><td className="font-medium">{a.action}</td><td className="text-xs">{a.entity_type} {a.entity_id?.slice(0, 12)}</td>
                <td><Badge tone={a.result === "OK" || a.result === "COMMITTED" ? "green" : "amber"}>{a.result}</Badge></td>
                <td className="text-xs text-muted-foreground max-w-xs truncate" title={JSON.stringify(a.data)}>{JSON.stringify(a.data)}</td></tr>
            ))}
          </Table>
        </Card>
      ) : (
        <Card>
          <CardTitle icon={<Package className="text-green-600" />}>Allocation ledger (mirror of /v1/allocations)</CardTitle>
          {(alloc.data ?? []).length === 0 && <Empty>No allocations yet</Empty>}
          <Table head={["#", "Route", "Fuel", "Qty", "Created", "Departed", "Arrival", "Status", "Origin", "Key"]}>
            {(alloc.data ?? []).map((a) => (
              <tr key={a.id}><td className="tabular">{a.id}</td><td className="text-xs">{a.route_id?.replace("route-", "")}</td><td>{a.fuel_type}</td>
                <td className="tabular">{fmtL(a.quantity)}</td><td className="tabular">{a.created_tick}</td><td className="tabular">{a.departure_tick ?? "—"}</td>
                <td className="tabular">{a.actual_arrival_tick ?? a.expected_arrival_tick ?? "—"}</td>
                <td><Status s={a.status} />{a.failure_reason && <span className="text-xs text-red-600 ml-1">{a.failure_reason}</span>}</td>
                <td className="text-xs">{a.system_origin ?? "external"}</td><td className="font-mono text-[11px]">{a.idempotency_key.slice(0, 16)}…</td></tr>
            ))}
          </Table>
        </Card>
      )}
    </>
  );
}
