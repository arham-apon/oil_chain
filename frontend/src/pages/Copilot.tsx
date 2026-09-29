import { useRef, useState } from "react";
import { Bot, Send, User, Wrench } from "lucide-react";
import { post } from "@/lib/api";
import { Badge, Button, Card, CardTitle, Input, PageHeader } from "@/components/ui";
import { cn } from "@/lib/utils";

type Msg = { role: "user" | "bot"; text: string; tools?: any[]; source?: string };
const EXAMPLES = [
  "Which station is closest to a stockout right now?",
  "If Gazipur depot throughput is degraded by 50% for the next 12 ticks, can Patiya sustain Mirpur's diesel without causing a stockout at Karnaphuli?",
  "What happens to Tongi if route-gazipur-tongi is disrupted?",
];

export default function Copilot() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const session = useRef(`ui-${Math.random().toString(36).slice(2, 9)}`);
  const send = async (m: string) => {
    if (!m.trim()) return;
    setMsgs((x) => [...x, { role: "user", text: m }]); setText(""); setBusy(true);
    try {
      const r = await post("/api/copilot/chat", { session_id: session.current, message: m });
      setMsgs((x) => [...x, { role: "bot", text: r.answer, tools: r.tool_calls, source: r.source }]);
    } catch (e: any) { setMsgs((x) => [...x, { role: "bot", text: `Error: ${e.message}`, source: "ERROR" }]); }
    finally { setBusy(false); }
  };
  return (
    <>
      <PageHeader title="Operator Copilot" sub="Ask questions about the network — answers come only from read-only tool calls; it can stage, never dispatch" />
      <Card className="h-[calc(100vh-14rem)]">
        <CardTitle icon={<Bot className="text-blue-600" />} right={<Badge tone="purple">SIMULATED DATA</Badge>}>Chat</CardTitle>
        <div className="flex-1 overflow-y-auto space-y-4 pr-1">
          {msgs.length === 0 && <div className="space-y-2"><p className="text-sm text-muted-foreground">Try one of these:</p>
            {EXAMPLES.map((e) => <button key={e} onClick={() => send(e)} className="block text-left text-sm rounded-lg border p-3 hover:bg-accent w-full">{e}</button>)}</div>}
          {msgs.map((m, i) => (
            <div key={i} className={cn("flex gap-3", m.role === "user" && "flex-row-reverse")}>
              <div className={cn("h-8 w-8 shrink-0 rounded-full flex items-center justify-center", m.role === "user" ? "bg-primary text-primary-foreground" : "bg-blue-600 text-white")}>{m.role === "user" ? <User className="size-4" /> : <Bot className="size-4" />}</div>
              <div className={cn("max-w-[75%] rounded-xl p-3 text-sm space-y-2", m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted/60")}>
                {m.tools?.map((t, k) => (
                  <details key={k} className="text-xs rounded-md bg-background/70 p-2">
                    <summary className="cursor-pointer flex items-center gap-1"><Wrench className="size-3" /><b>{t.tool}</b>({Object.keys(t.args || {}).join(", ")}) · {t.ms} ms {t.ok ? "" : "· failed"}</summary>
                    <pre className="whitespace-pre-wrap break-all mt-1 text-[11px]">{JSON.stringify(t.args)}{"\n"}{t.result_preview}</pre>
                  </details>
                ))}
                <p className="whitespace-pre-wrap">{m.text}</p>
                {m.source && m.source !== "GEMINI" && m.role === "bot" && <Badge tone="amber">{m.source}</Badge>}
              </div>
            </div>
          ))}
          {busy && <p className="text-sm text-muted-foreground">Thinking… (calling tools)</p>}
        </div>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); send(text); }}>
          <Input className="flex-1 h-10" value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask about stock, routes, forecasts, what-ifs…" />
          <Button variant="default" className="h-10" disabled={busy}><Send />Send</Button>
        </form>
      </Card>
    </>
  );
}
