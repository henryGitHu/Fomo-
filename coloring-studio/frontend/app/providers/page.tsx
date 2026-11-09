"use client";

import { useState } from "react";
import { Button, Card, ErrorNote, Field, inputBase, inputCls } from "@/components/ui";
import { Provider, money, post, put } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

function StatusPill({ p }: { p: Provider }) {
  const map: Record<string, string> = {
    simulated: "bg-violet-100 text-violet-800", ready: "bg-emerald-100 text-emerald-800", untested: "bg-amber-100 text-amber-900",
    needs_key: "bg-slate-100 text-slate-700", disabled: "bg-slate-200 text-slate-600", error: "bg-rose-100 text-rose-800",
  };
  const s = p.kind === "text" ? (p.key_source === "none" ? "needs_key" : "ready") : p.status || "";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${map[s] || "bg-slate-100"}`}>{s.replace("_", " ")}</span>;
}

function ProviderCard({ p, onChange }: { p: Provider; onChange: () => void }) {
  const [key, setKey] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction();
  const save = (body: unknown) => act.run(async () => { await put(`/providers/${p.provider_id}`, body); onChange(); });
  return (
    <Card title={<span className="flex items-center gap-2">{p.display_name} <StatusPill p={p} /></span>}
      actions={p.docs_url ? <a className="text-xs text-brand-700 underline" href={p.docs_url} target="_blank" rel="noreferrer">API docs</a> : null}>
      <ErrorNote error={act.error} />
      {p.is_mock && <p className="mb-2 rounded bg-violet-50 px-2 py-1 text-xs text-violet-900">Offline simulation: draws procedural line art locally. Use for testing workflows — not for publishing.</p>}
      <div className="grid gap-3 text-sm sm:grid-cols-2">
        {(p.requires_api_key || p.kind === "text") && (
          <Field label={`API key ${p.key_masked ? `(saved: ${p.key_masked}, from ${p.key_source})` : "(not set)"}`} hint="Stored encrypted on the server; never shown again.">
            <div className="flex gap-2">
              <input type="password" autoComplete="off" className={inputCls} value={key} onChange={(e) => setKey(e.target.value)} placeholder="Paste key" aria-label={`${p.display_name} API key`} />
              <Button disabled={!key || act.busy} onClick={() => { save({ api_key: key }); setKey(""); }}>Save</Button>
            </div>
          </Field>
        )}
        {p.kind === "image" && (
          <>
            <Field label="Default model">
              <select className={inputCls} value={p.default_model || ""} onChange={(e) => save({ default_model: e.target.value })}>
                {p.models.map((m) => <option key={m.model_id} value={m.model_id}>{m.display_name}</option>)}
              </select>
            </Field>
            <Field label="Priority (lower = preferred)">
              <input type="number" className={inputCls} defaultValue={p.priority} onBlur={(e) => save({ priority: Number(e.target.value) })} />
            </Field>
          </>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={p.enabled} onChange={(e) => save({ enabled: e.target.checked })} /> Enabled</label>
        {p.kind === "image" && <Button variant="secondary" disabled={act.busy} onClick={() => act.run(async () => { const r = await post(`/providers/${p.provider_id}/test`); setMsg(`${r.ok ? "✓" : "✕"} ${r.message}${r.live ? "" : " (simulated)"}`); onChange(); })}>Test connection</Button>}
        {p.key_masked && p.key_source === "stored" && <Button variant="ghost" onClick={() => save({ clear_key: true })}>Remove key</Button>}
        {msg && <span className="text-sm" role="status">{msg}</span>}
        {p.last_test_at && <span className="text-xs text-slate-500">Last test: {new Date(p.last_test_at).toLocaleString()} {p.last_test_ok ? "✓" : "✕"}</span>}
        {p.last_success_at && <span className="text-xs text-slate-500">Last generation: {new Date(p.last_success_at).toLocaleString()}</span>}
      </div>
      {p.kind === "image" && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500"><tr><th className="py-1 pr-2">Model</th><th className="pr-2">Capabilities</th><th className="pr-2">Max side</th><th className="pr-2">Price/image</th><th className="pr-2">Measured quality</th><th className="pr-2">Avg latency</th><th>Samples</th></tr></thead>
            <tbody>
              {p.models.map((m) => (
                <tr key={m.model_id} className="border-t border-slate-100 align-top">
                  <td className="py-1 pr-2 font-medium">{m.display_name}<div className="text-[10px] text-slate-400">{m.model_id}</div></td>
                  <td className="pr-2">{["text→image", m.image_edit && "edit", m.mask_edit && "mask", m.reference_images && "references", m.negative_prompt && "negative", m.seed && "seed", m.async_jobs && "async"].filter(Boolean).join(", ")}</td>
                  <td className="pr-2">{m.max_side}px</td>
                  <td className="pr-2" title={m.pricing_note}>{money(m.price_per_image)}<div className="text-[10px] text-slate-400">{m.pricing_note}</div></td>
                  <td className="pr-2">{m.measured_avg_quality != null ? m.measured_avg_quality.toFixed(0) : "—"}</td>
                  <td className="pr-2">{m.measured_avg_latency != null ? `${m.measured_avg_latency.toFixed(2)}s` : "—"}</td>
                  <td>{m.samples}{m.measured_failure_rate ? ` (${Math.round(m.measured_failure_rate * 100)}% fail)` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Benchmarks({ providers }: { providers: Provider[] }) {
  const usable = providers.filter((p) => p.kind === "image" && p.usable);
  const [sel, setSel] = useState<string[]>([]);
  const [cap, setCap] = useState("1.00");
  const res = useResource<any>("/benchmarks", 4000);
  const act = useAction();
  return (
    <Card title="Provider benchmark" actions={<span className="text-xs text-slate-500">Standard coloring-book suite; results feed the smart router</span>}>
      <ErrorNote error={act.error} />
      <div className="flex flex-wrap items-end gap-3 text-sm">
        {usable.map((p) => (
          <label key={p.provider_id} className="flex items-center gap-1"><input type="checkbox" checked={sel.includes(p.provider_id)} onChange={(e) => setSel((s) => e.target.checked ? [...s, p.provider_id] : s.filter((x) => x !== p.provider_id))} />{p.provider_id}</label>
        ))}
        <Field label="Max spend (USD)"><input className={inputBase + " w-28"} value={cap} onChange={(e) => setCap(e.target.value)} /></Field>
        <Button disabled={!sel.length || act.busy} onClick={() => act.run(async () => { await post("/benchmarks", { contenders: sel.map((s) => ({ provider_id: s })), max_spend: Number(cap) }); res.reload(); })}>Run benchmark</Button>
      </div>
      {res.data?.summary?.length ? (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-left text-slate-500"><th className="py-1">Provider/model</th>{res.data.suite.map((c: string) => <th key={c} className="px-1">{c.replace(/_/g, " ")}</th>)}</tr></thead>
            <tbody>{res.data.summary.map((s: any) => (
              <tr key={s.provider_id + s.model_id} className="border-t border-slate-100">
                <td className="py-1 font-medium">{s.provider_id}/{s.model_id}</td>
                {res.data.suite.map((c: string) => <td key={c} className="px-1">{s.categories[c]?.overall ?? (s.categories[c]?.error ? "error" : "—")}</td>)}
              </tr>))}
            </tbody>
          </table>
        </div>
      ) : <p className="mt-2 text-sm text-slate-500">No benchmark results yet.</p>}
    </Card>
  );
}

export default function ControlCenter() {
  const { data, error, reload } = useResource<Provider[]>("/providers");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">AI Model Control Center</h1>
        <p className="text-sm text-slate-600">Connect image providers, choose default models, test connections and compare measured performance. Per-book routing (manual, smart auto-router, quality-first) and budgets are set in each book&apos;s Settings.</p>
      </div>
      <ErrorNote error={error} />
      <div className="grid gap-4 xl:grid-cols-2">{data?.map((p) => <ProviderCard key={p.provider_id} p={p} onChange={reload} />)}</div>
      {data && <Benchmarks providers={data} />}
    </div>
  );
}
