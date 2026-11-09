"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useProject } from "@/components/project-context";
import { Button, Card, ErrorNote, Field, inputBase, inputCls } from "@/components/ui";
import { Provider, Section, del, patch, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

function SectionEditor({ s, providers, onDone, first, last, onMove }: {
  s: Section; providers: Provider[]; onDone: () => void; first: boolean; last: boolean; onMove: (d: -1 | 1) => void;
}) {
  const act = useAction();
  const [f, setF] = useState({ name: s.name, illustration_count: s.illustration_count, theme_instructions: s.theme_instructions,
    complexity: s.complexity, educational_goals: s.educational_goals, excluded_content: s.excluded_content,
    preferred_provider: s.preferred_provider || "" });
  useEffect(() => { setF({ name: s.name, illustration_count: s.illustration_count, theme_instructions: s.theme_instructions,
    complexity: s.complexity, educational_goals: s.educational_goals, excluded_content: s.excluded_content,
    preferred_provider: s.preferred_provider || "" }); }, [s.version]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k: string, v: unknown) => setF((x) => ({ ...x, [k]: v }));
  const run = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); onDone(); });
  return (
    <Card title={<span>Section {s.position} <span className="text-xs font-normal text-slate-500">pages {s.page_positions?.[0]}–{s.page_positions?.[s.page_positions.length - 1]} · {s.learning_stage} · {s.approved} approved</span></span>}
      actions={<>
        <Button variant="ghost" disabled={first} onClick={() => onMove(-1)} aria-label="Move up">↑</Button>
        <Button variant="ghost" disabled={last} onClick={() => onMove(1)} aria-label="Move down">↓</Button>
        <Button variant="ghost" onClick={() => run(() => post(`/sections/${s.id}/duplicate`))}>Duplicate</Button>
        <Button variant="ghost" onClick={() => run(() => post(`/sections/${s.id}/action`, { action: "generate" }))}>Generate</Button>
        <Button variant="ghost" onClick={() => run(() => post(`/sections/${s.id}/action`, { action: "approve" }))}>Approve all</Button>
        <Button variant="ghost" onClick={() => run(() => post(`/sections/${s.id}/action`, { action: "lock" }))}>Lock</Button>
        <Button variant="ghost" className="text-rose-700" onClick={() => window.confirm(`Remove section “${s.name}” and its unapproved pages?`) && run(() => del(`/sections/${s.id}`))}>Remove</Button>
      </>}>
      <ErrorNote error={act.error} />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Name"><input className={inputCls} value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Illustrations"><input type="number" min={1} className={inputCls} value={f.illustration_count} onChange={(e) => set("illustration_count", Number(e.target.value))} /></Field>
        <Field label="Visual complexity"><select className={inputCls} value={f.complexity} onChange={(e) => set("complexity", e.target.value)}><option>low</option><option>medium</option><option>high</option></select></Field>
        <Field label="Theme instructions"><textarea rows={2} className={inputCls} value={f.theme_instructions} onChange={(e) => set("theme_instructions", e.target.value)} /></Field>
        <Field label="Educational goals"><textarea rows={2} className={inputCls} value={f.educational_goals} onChange={(e) => set("educational_goals", e.target.value)} /></Field>
        <Field label="Excluded content (comma separated)"><textarea rows={2} className={inputCls} value={f.excluded_content} onChange={(e) => set("excluded_content", e.target.value)} /></Field>
        <Field label="Preferred AI provider (optional)">
          <select className={inputCls} value={f.preferred_provider} onChange={(e) => set("preferred_provider", e.target.value)}>
            <option value="">Book default / router</option>
            {providers.filter((p) => p.kind === "image" && p.usable).map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}
          </select>
        </Field>
      </div>
      <div className="mt-3"><Button disabled={act.busy} onClick={() => run(() => patch(`/sections/${s.id}`, {
        version: s.version, ...f, preferred_provider: f.preferred_provider || undefined, clear_provider: !f.preferred_provider,
      }))}>Save section</Button></div>
    </Card>
  );
}

export default function Planner() {
  const { id } = useParams<{ id: string }>();
  const { project, reload, bump } = useProject();
  const providers = useResource<Provider[]>("/providers");
  const [count, setCount] = useState<number | null>(null);
  const rec = useResource<any>(`/projects/${id}/sections/recommend${count ? `?count=${count}` : ""}`);
  const act = useAction();
  const [k, setK] = useState<number | null>(null);
  const [preview, setPreview] = useState<any[] | null>(null);
  const secs = project?.sections || [];
  const total = secs.reduce((a, s) => a + s.illustration_count, 0);
  const done = () => { reload(); bump(); };
  const n = count ?? project?.illustration_count ?? 0;

  async function move(i: number, d: -1 | 1) {
    const ids = secs.map((s) => s.id);
    [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
    await act.run(() => post(`/projects/${id}/sections/reorder`, { section_ids: ids }));
    done();
  }

  return (
    <div className="space-y-4">
      <Card title="Dynamic Section Builder">
        <ErrorNote error={act.error} />
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <Field label="Illustrations"><input type="number" min={1} max={400} className={inputBase + " w-28"} value={n} onChange={(e) => setCount(Number(e.target.value))} /></Field>
          <Field label={`Sections (recommended ${rec.data?.recommended ?? "…"}; range ${rec.data?.min ?? ""}–${rec.data?.max ?? ""})`}>
            <input type="number" min={1} className={inputBase + " w-28"} value={k ?? rec.data?.recommended ?? ""} onChange={(e) => setK(Number(e.target.value))} />
          </Field>
          <Button variant="secondary" disabled={act.busy} onClick={() => act.run(async () => {
            const r = await post(`/projects/${id}/plan`, { sections: k ?? rec.data?.recommended, illustration_count: n, dry_run: true }); setPreview(r.plan);
          })}>Preview plan</Button>
          <Button disabled={act.busy} onClick={() => act.run(async () => {
            if (secs.length && !window.confirm("Replace the current plan? (Refused automatically if any page already has artwork.)")) return;
            await post(`/projects/${id}/plan`, { sections: k ?? rec.data?.recommended, illustration_count: n }); setPreview(null); done();
          })}>Build plan from research</Button>
          <Button variant="secondary" onClick={() => act.run(async () => { await post(`/projects/${id}/sections`, { name: "New section", illustration_count: 1 }); done(); })}>+ Add section</Button>
        </div>
        <p className={`mt-2 text-sm ${total === project?.illustration_count ? "text-emerald-700" : "text-rose-700"}`}>
          Sections allocate {total} of {project?.illustration_count} illustrations {total === project?.illustration_count ? "✓" : "— must match exactly"}.
          {project?.education_progression ? " Learning progression: introduction → tools → processes → applications → careers." : ""}
        </p>
        {preview && (
          <div className="mt-3 rounded-xl bg-slate-50 p-3 text-sm">
            <p className="mb-1 font-medium">Plan preview (not saved)</p>
            <ol className="list-decimal pl-5">{preview.map((s) => <li key={s.name}>{s.name} — {s.illustration_count} pages: <span className="text-xs text-slate-600">{s.pages.slice(0, 4).map((p: any) => p.subject).join("; ")}{s.pages.length > 4 ? "…" : ""}</span></li>)}</ol>
          </div>
        )}
      </Card>
      {secs.map((s, i) => (
        <SectionEditor key={s.id} s={s} providers={providers.data || []} onDone={done} first={i === 0} last={i === secs.length - 1} onMove={(d) => move(i, d)} />
      ))}
    </div>
  );
}
