"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import JobsBar from "@/components/JobsBar";
import PageDetail from "@/components/PageDetail";
import { useProject } from "@/components/project-context";
import { Button, Card, Empty, ErrorNote, StatusBadge, inputBase, inputCls } from "@/components/ui";
import { Page, Provider, STATUS_LABEL, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function Studio() {
  const { id } = useParams<{ id: string }>();
  const { project, tick, bump } = useProject();
  const { data: pages, reload } = useResource<Page[]>(`/projects/${id}/pages`, 4000);
  const providers = useResource<Provider[]>("/providers");
  const act = useAction();
  const [selected, setSelected] = useState<string[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [batch, setBatch] = useState<number>(5);
  const [prov, setProv] = useState("");
  const [filter, setFilter] = useState("all");
  useEffect(() => { reload(); }, [tick, reload]);
  useEffect(() => { if (project) setBatch(project.batch_size); }, [project?.batch_size]); // eslint-disable-line react-hooks/exhaustive-deps

  const sections = project?.sections || [];
  const usable = (providers.data || []).filter((p) => p.kind === "image" && p.usable);
  const shown = useMemo(() => (pages || []).filter((p) => filter === "all" || p.status === filter), [pages, filter]);
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    (pages || []).forEach((p) => { c[p.status] = (c[p.status] || 0) + 1; });
    return c;
  }, [pages]);
  const toggle = (pid: string) => setSelected((s) => s.includes(pid) ? s.filter((x) => x !== pid) : [...s, pid]);
  const refresh = () => { reload(); bump(); };
  const bulk = (fn: (p: Page) => Promise<unknown>) => act.run(async () => {
    for (const p of (pages || []).filter((x) => selected.includes(x.id))) await fn(p).catch(() => {});
    setSelected([]);
    refresh();
  });

  if (pages && pages.length === 0) {
    return <Empty>No pages planned yet. Run <a className="underline" href={`/projects/${id}/research`}>Smart Research</a> and then the <a className="underline" href={`/projects/${id}/plan`}>Section Planner</a>.</Empty>;
  }
  return (
    <div className="space-y-4">
      <Card title="Illustration Studio" actions={
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label className="flex items-center gap-1">Batch
            <select className={inputBase + " w-auto"} value={batch} onChange={(e) => setBatch(Number(e.target.value))} aria-label="Batch size">
              {[1, 5, 10, 20].concat(project && ![1, 5, 10, 20].includes(project.batch_size) ? [project.batch_size] : []).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <select className={inputBase + " w-auto"} value={prov} onChange={(e) => setProv(e.target.value)} aria-label="Provider for this batch">
            <option value="">Book routing ({project?.generation_mode})</option>
            {usable.map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}
          </select>
          <Button disabled={act.busy} onClick={() => act.run(async () => { await post(`/projects/${id}/generate`, { count: batch, provider_id: prov || null }); refresh(); })}>
            Generate next {batch}
          </Button>
        </div>}>
        <p className="text-sm text-slate-600">
          {project?.autopilot ? "Autopilot: batches continue automatically within your budget and retry limits; final publication still needs your approval." :
            "Guided mode: review each batch, approve or request revisions, then generate the next batch."}
        </p>
        <ErrorNote error={act.error} />
        <div className="mt-3 flex flex-wrap gap-1 text-xs">
          {["all", ...Object.keys(STATUS_LABEL)].filter((s) => s === "all" || counts[s]).map((s) => (
            <button key={s} onClick={() => setFilter(s)} className={`rounded-full px-2 py-0.5 ${filter === s ? "bg-ink text-white" : "bg-slate-100"}`}>
              {s === "all" ? `All (${pages?.length || 0})` : `${STATUS_LABEL[s]} (${counts[s]})`}
            </button>
          ))}
        </div>
      </Card>
      <JobsBar projectId={id} />
      {selected.length > 0 && (
        <div className="sticky top-14 z-20 flex flex-wrap items-center gap-2 rounded-xl border border-brand-100 bg-brand-50 p-2 text-sm">
          <span className="font-medium">{selected.length} selected</span>
          <Button onClick={() => bulk((p) => post(`/pages/${p.id}/approve`))}>Approve</Button>
          <Button variant="secondary" onClick={() => bulk((p) => post(`/pages/${p.id}/reject`))}>Request revision</Button>
          <Button variant="secondary" onClick={() => bulk((p) => post(`/pages/${p.id}/lock`))}>Lock</Button>
          <Button variant="secondary" onClick={() => act.run(async () => {
            await post(`/projects/${id}/generate`, { page_ids: selected, provider_id: prov || null }); setSelected([]); refresh();
          })}>Regenerate (skips approved/locked)</Button>
          <Button variant="ghost" onClick={() => setSelected([])}>Clear</Button>
        </div>
      )}
      {sections.map((s) => {
        const sp = shown.filter((p) => p.section_id === s.id);
        if (!sp.length) return null;
        return (
          <section key={s.id} aria-label={s.name}>
            <h2 className="mb-2 text-sm font-semibold text-ink-soft">Section {s.position}: {s.name} <span className="font-normal">· {s.approved}/{s.illustration_count} approved{s.preferred_provider ? ` · uses ${s.preferred_provider}` : ""}</span></h2>
            <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
              {sp.map((p) => (
                <li key={p.id} className={`group relative rounded-xl border bg-white p-2 shadow-sm ${selected.includes(p.id) ? "border-brand-500 ring-2 ring-brand-100" : "border-slate-200"}`} data-testid={`page-card-${p.position}`}>
                  <label className="absolute left-3 top-3 z-10"><input type="checkbox" checked={selected.includes(p.id)} onChange={() => toggle(p.id)} aria-label={`Select page ${p.position}`} /></label>
                  <button onClick={() => setOpen(p.id)} className="block w-full text-left" aria-label={`Open page ${p.position}: ${p.subject}`}>
                    <div className="checker relative grid aspect-[3/4] place-items-center overflow-hidden rounded-lg border border-slate-100 bg-white">
                      {p.current_revision?.image_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={p.current_revision.image_url} alt="" loading="lazy" className="h-full w-full bg-white object-contain" />
                      ) : <span className="px-2 text-center text-xs text-slate-400">{p.status === "generating" ? "Generating…" : p.status === "queued" ? "Queued" : "Not generated"}</span>}
                      {p.locked && <span className="absolute right-1 top-1 text-sm" title="Locked">🔒</span>}
                      {p.current_revision && p.current_revision.qa_passed === false && <span className="absolute bottom-1 right-1 rounded bg-rose-600 px-1 text-[10px] text-white">QA</span>}
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-1">
                      <span className="truncate text-xs font-medium">{p.position}. {p.subject}</span>
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-1">
                      <StatusBadge status={p.status} />
                      {p.current_revision && <span className="truncate text-[10px] text-slate-500">{p.current_revision.provider_id}</span>}
                    </div>
                    {p.caption_text && <p className="mt-1 line-clamp-2 text-[11px] text-slate-600">{p.caption_text}</p>}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
      {open && (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/30" role="dialog" aria-modal="true" aria-label="Page detail" onClick={() => setOpen(null)}>
          <div className="h-full w-full max-w-4xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <PageDetail pageId={open} onClose={() => { setOpen(null); refresh(); }} />
          </div>
        </div>
      )}
    </div>
  );
}
