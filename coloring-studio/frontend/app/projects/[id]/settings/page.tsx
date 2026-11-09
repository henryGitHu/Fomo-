"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useProject } from "@/components/project-context";
import { Button, Card, ErrorNote, Field, inputCls } from "@/components/ui";
import { Provider, patch, post, put } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function Settings() {
  const { id } = useParams<{ id: string }>();
  const { project, reload } = useProject();
  const providers = useResource<Provider[]>("/providers");
  const style = useResource<any>(`/projects/${id}/style`);
  const route = useResource<any>(`/projects/${id}/route-preview`);
  const act = useAction();
  const [f, setF] = useState<Record<string, any>>({});
  const [styleData, setStyleData] = useState<Record<string, any>>({});
  useEffect(() => { if (project) setF({ ...project, budget: project.budget_limit ?? "" }); }, [project?.version]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (style.data) setStyleData(style.data.data); }, [style.data?.version]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!project) return <p>Loading…</p>;
  const set = (k: string, v: unknown) => setF((x) => ({ ...x, [k]: v }));
  const usable = (providers.data || []).filter((p) => p.kind === "image" && p.usable);
  const models = usable.find((p) => p.provider_id === f.default_provider)?.models || [];

  const save = () => act.run(async () => {
    await patch(`/projects/${id}`, {
      version: project.version, title: f.title, subtitle: f.subtitle, author: f.author, theme: f.theme,
      audience_age: f.audience_age, trim_size: f.trim_size, paper_type: f.paper_type, bleed: f.bleed,
      generation_mode: f.generation_mode, routing_preference: f.routing_preference, default_provider: f.default_provider,
      default_model: f.default_model, batch_size: Number(f.batch_size), autopilot: f.autopilot,
      max_retries: Number(f.max_retries), fallback_enabled: f.fallback_enabled, enhance_images: f.enhance_images,
      education_progression: f.education_progression, back_description: f.back_description,
      ...(f.budget === "" ? { clear_budget: true } : { budget_limit: Number(f.budget) }),
    });
    reload(); route.reload();
  });

  return (
    <div className="space-y-4">
      <ErrorNote error={act.error} />
      <Card title="Book details">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {["title", "subtitle", "author", "theme"].map((k) => <Field key={k} label={k}><input className={inputCls} value={f[k] ?? ""} onChange={(e) => set(k, e.target.value)} /></Field>)}
          <Field label="Target age"><select className={inputCls} value={f.audience_age} onChange={(e) => set("audience_age", e.target.value)}>{["3-5", "4-8", "6-9", "8-12", "10-14", "adult"].map((a) => <option key={a}>{a}</option>)}</select></Field>
          <Field label="Trim size"><select className={inputCls} value={f.trim_size} onChange={(e) => set("trim_size", e.target.value)}>{["8.5x11", "8.25x11", "8x10", "8.5x8.5", "8.25x8.25", "7x10", "6x9"].map((t) => <option key={t}>{t}</option>)}</select></Field>
          <Field label="Paper"><select className={inputCls} value={f.paper_type} onChange={(e) => set("paper_type", e.target.value)}><option value="white">White</option><option value="cream">Cream</option></select></Field>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!f.bleed} onChange={(e) => set("bleed", e.target.checked)} /> Interior bleed</label>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!f.education_progression} onChange={(e) => set("education_progression", e.target.checked)} /> Educational progression</label>
          <Field label="Back-cover description"><textarea rows={3} className={inputCls} value={f.back_description ?? ""} onChange={(e) => set("back_description", e.target.value)} /></Field>
        </div>
      </Card>
      <Card title="Generation mode & routing">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Mode" hint="Manual: always the default provider · Auto: router picks by measured quality, cost, latency, resolution · Quality-first: best model within budget, one bounded repair attempt on QA failure">
            <select className={inputCls} value={f.generation_mode} onChange={(e) => set("generation_mode", e.target.value)}><option value="manual">Manual selection</option><option value="auto">Smart auto-router</option><option value="quality_first">Quality-first</option></select>
          </Field>
          <Field label="Cost preference"><select className={inputCls} value={f.routing_preference} onChange={(e) => set("routing_preference", e.target.value)}><option value="lowest_cost">Lowest estimated cost</option><option value="balanced">Balanced quality & cost</option><option value="highest_quality">Highest quality within budget</option></select></Field>
          <Field label="Default provider"><select className={inputCls} value={f.default_provider} onChange={(e) => { set("default_provider", e.target.value); set("default_model", usable.find((p) => p.provider_id === e.target.value)?.models[0]?.model_id); }}>{usable.map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}</select></Field>
          <Field label="Default model"><select className={inputCls} value={f.default_model} onChange={(e) => set("default_model", e.target.value)}>{models.map((m) => <option key={m.model_id} value={m.model_id}>{m.display_name}</option>)}</select></Field>
          <Field label="Batch size"><input type="number" min={1} max={50} className={inputCls} value={f.batch_size ?? 5} onChange={(e) => set("batch_size", e.target.value)} /></Field>
          <Field label="Retry limit per image"><input type="number" min={0} max={5} className={inputCls} value={f.max_retries ?? 2} onChange={(e) => set("max_retries", e.target.value)} /></Field>
          <Field label="Spending limit (USD, empty = none)"><input type="number" min={0} step="0.01" className={inputCls} value={f.budget} onChange={(e) => set("budget", e.target.value)} /></Field>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!f.autopilot} onChange={(e) => set("autopilot", e.target.checked)} /> Autopilot (continue batches automatically)</label>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!f.fallback_enabled} onChange={(e) => set("fallback_enabled", e.target.checked)} /> Fall back to another provider on failure (respects budget & style)</label>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!f.enhance_images} onChange={(e) => set("enhance_images", e.target.checked)} /> Print enhancement (cleanup, upscale, sharpen)</label>
        </div>
        <div className="mt-3"><Button disabled={act.busy} onClick={save}>Save settings</Button></div>
        {route.data && (
          <div className="mt-3 text-xs text-slate-600">
            <p className="font-medium">Router ranking now ({route.data.preference}):</p>
            <ol className="list-decimal pl-5">{route.data.ranking.slice(0, 6).map((r: any) => <li key={r.provider_id + r.model_id}>{r.provider_id}/{r.model_id} — score {r.score} ({r.reason})</li>)}</ol>
          </div>
        )}
      </Card>
      <Card title="Visual Style DNA" actions={<Button variant="secondary" onClick={() => act.run(async () => { const name = window.prompt("Save this style to the library as:"); if (name) await post("/styles", { project_id: id, name }); })}>Save to style library</Button>}>
        <div className="grid gap-3 sm:grid-cols-2">
          {["art_style", "line_thickness", "outline", "proportions", "detail_density", "background_complexity", "composition", "whitespace"].map((k) => (
            <Field key={k} label={k.replace(/_/g, " ")}><input className={inputCls} value={styleData[k] ?? ""} onChange={(e) => setStyleData((s) => ({ ...s, [k]: e.target.value }))} /></Field>
          ))}
          <Field label="Exclusions (comma separated)"><input className={inputCls} value={(styleData.exclusions || []).join(", ")} onChange={(e) => setStyleData((s) => ({ ...s, exclusions: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) }))} /></Field>
          <Field label="Approved character designs (one per line)"><textarea rows={3} className={inputCls} value={(styleData.character_designs || []).join("\n")} onChange={(e) => setStyleData((s) => ({ ...s, character_designs: e.target.value.split("\n").filter(Boolean) }))} /></Field>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button onClick={() => act.run(async () => { await put(`/projects/${id}/style`, { data: styleData, version: style.data.version }); style.reload(); })}>Save style</Button>
          <span className="text-xs text-slate-500">Style anchor: {style.data?.anchor_revision_ids?.length || 0} approved image(s). New images are measured against it (stroke weight, ink density, detail) and flagged if they drift. Set anchors from approved pages below.</span>
        </div>
        <AnchorPicker projectId={id} onSet={style.reload} current={style.data?.anchor_revision_ids || []} />
      </Card>
    </div>
  );
}

function AnchorPicker({ projectId, onSet, current }: { projectId: string; onSet: () => void; current: string[] }) {
  const pages = useResource<any[]>(`/projects/${projectId}/pages`);
  const approved = (pages.data || []).filter((p) => p.current_revision && ["approved", "locked", "export_ready"].includes(p.status));
  const [sel, setSel] = useState<string[]>([]);
  useEffect(() => setSel(current), [current.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!approved.length) return <p className="mt-2 text-xs text-slate-500">Approve some pages to choose style anchors.</p>;
  return (
    <div className="mt-3">
      <div className="flex gap-2 overflow-x-auto pb-2">
        {approved.map((p) => {
          const rid = p.current_revision.id;
          return (
            <label key={rid} className={`w-20 shrink-0 cursor-pointer rounded border p-1 text-[10px] ${sel.includes(rid) ? "border-brand-500 ring-2 ring-brand-100" : "border-slate-200"}`}>
              <input type="checkbox" className="sr-only" checked={sel.includes(rid)} onChange={(e) => setSel((s) => e.target.checked ? [...s, rid] : s.filter((x) => x !== rid))} />
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={p.current_revision.image_url} alt={`Page ${p.position}`} className="aspect-[3/4] w-full bg-white object-contain" />
              p.{p.position}
            </label>
          );
        })}
      </div>
      <Button variant="secondary" disabled={!sel.length} onClick={async () => { await post(`/projects/${projectId}/style/anchor`, { revision_ids: sel }); onSet(); }}>Use selected as style anchor</Button>
    </div>
  );
}
