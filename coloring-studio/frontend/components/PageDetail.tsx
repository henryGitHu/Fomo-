"use client";

import { Fragment, useEffect, useState } from "react";
import { Page, Provider, money, patch, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";
import { Button, CheckRow, ErrorNote, Field, StatusBadge, inputCls } from "./ui";
import { useProject } from "./project-context";

export default function PageDetail({ pageId, onClose }: { pageId: string; onClose: () => void }) {
  const { tick, bump } = useProject();
  const { data: page, reload, error } = useResource<Page>(`/pages/${pageId}`);
  const providers = useResource<Provider[]>("/providers");
  const act = useAction();
  const [caption, setCaption] = useState("");
  const [subject, setSubject] = useState("");
  const [prov, setProv] = useState("");
  useEffect(() => { reload(); }, [tick, reload]);
  useEffect(() => {
    if (page) { setCaption(page.caption_text); setSubject(page.subject); }
  }, [page?.id, page?.caption_version, page?.version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!page) return <div className="p-4 text-sm">{error || "Loading…"}</div>;
  const rev = page.current_revision;
  const usable = (providers.data || []).filter((p) => p.kind === "image" && p.usable);
  const done = () => { reload(); bump(); };
  const action = (path: string, body: unknown = {}) => act.run(async () => { await post(path, body); done(); });

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-slate-100 p-3">
        <h2 className="font-semibold">Page {page.position}: {page.subject}</h2>
        <StatusBadge status={page.status} />
        {page.locked && <span title="Locked">🔒</span>}
        <button onClick={onClose} className="ml-auto rounded px-2 text-lg text-slate-500 hover:bg-slate-100" aria-label="Close page detail">×</button>
      </div>
      <div className="flex-1 space-y-4 overflow-y-auto p-3">
        <ErrorNote error={act.error} />
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_260px]">
          <div className="checker grid min-h-[300px] place-items-center rounded-xl border border-slate-200 bg-white">
            {rev?.image_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={rev.image_url} alt={`Illustration: ${page.subject}`} className="max-h-[60vh] bg-white object-contain" />
            ) : <span className="text-sm text-slate-400">Not generated yet</span>}
          </div>
          <div className="space-y-2 text-sm">
            {rev && (
              <dl className="grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
                <dt className="text-slate-500">Provider</dt><dd>{rev.provider_id}</dd>
                <dt className="text-slate-500">Model</dt><dd className="break-all">{rev.model_id}</dd>
                <dt className="text-slate-500">Size</dt><dd>{rev.width}×{rev.height}px</dd>
                <dt className="text-slate-500">Print PPI</dt><dd>{rev.effective_ppi}{rev.native_ppi ? ` (native ${rev.native_ppi})` : ""}</dd>
                <dt className="text-slate-500">Cost</dt><dd>{money(rev.cost)}{rev.cost_is_estimate ? " (est.)" : ""}</dd>
                <dt className="text-slate-500">Revision</dt><dd>{rev.kind} · {rev.id.slice(0, 8)}</dd>
                {Object.entries(rev.scores || {}).map(([k, v]) => (<Fragment key={k}><dt className="text-slate-500">{k.replace(/_/g, " ")}</dt><dd>{v}</dd></Fragment>))}
              </dl>
            )}
            <div className="flex flex-wrap gap-1 pt-2">
              {!page.locked && rev && page.status !== "approved" && <Button onClick={() => action(`/pages/${page.id}/approve`)}>Approve</Button>}
              {!page.locked && rev && <Button variant="secondary" onClick={() => action(`/pages/${page.id}/reject`)}>Request revision</Button>}
              {rev && !page.locked && <Button variant="secondary" onClick={() => action(`/pages/${page.id}/lock`)}>🔒 Lock</Button>}
              {page.locked && <Button variant="secondary" onClick={() => action(`/pages/${page.id}/unlock`)}>Unlock</Button>}
            </div>
            {!page.locked && (
              <div className="space-y-2 border-t border-slate-100 pt-2">
                <Field label="Generate with">
                  <select className={inputCls} value={prov} onChange={(e) => setProv(e.target.value)}>
                    <option value="">Book routing (default)</option>
                    {usable.map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}
                  </select>
                </Field>
                <Button disabled={act.busy} onClick={() => act.run(async () => {
                  const approved = ["approved", "export_ready"].includes(page.status);
                  if (approved && !window.confirm("This page is approved. Create a new version for review? (The current one stays in history.)")) return;
                  await post(`/pages/${page.id}/generate`, { provider_id: prov || null, allow_approved: approved }); done();
                })}>{rev ? "Regenerate" : "Generate"}</Button>
                {rev && (
                  <div className="flex gap-1">
                    <Button variant="secondary" onClick={() => action(`/pages/${page.id}/line-weight`, { steps: 1 })}>Thicker lines</Button>
                    <Button variant="secondary" onClick={() => action(`/pages/${page.id}/line-weight`, { steps: -1 })}>Thinner</Button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <section className="grid gap-3 md:grid-cols-2">
          <div className="space-y-2">
            <Field label="Subject (changing it flags the page for regeneration)">
              <input className={inputCls} value={subject} disabled={page.locked} onChange={(e) => setSubject(e.target.value)} />
            </Field>
            <Field label="Educational caption (editing never regenerates the artwork)">
              <textarea rows={3} className={inputCls} value={caption} disabled={page.locked} onChange={(e) => setCaption(e.target.value)} />
            </Field>
            <div className="flex gap-2">
              <Button disabled={page.locked || act.busy} onClick={() => act.run(async () => {
                await patch(`/pages/${page.id}`, { version: page.version, caption_text: caption !== page.caption_text ? caption : undefined, subject: subject !== page.subject ? subject : undefined });
                done();
              })}>Save text</Button>
              <Button variant="secondary" disabled={page.locked} onClick={() => action(`/pages/${page.id}/caption/regenerate`)}>Regenerate caption</Button>
            </div>
            {page.caption_validation && <ul className="space-y-1">{page.caption_validation.checks.map((c) => <CheckRow key={c.id} c={c} />)}</ul>}
            {page.fact && <p className="text-xs text-slate-500">Planned fact: {page.fact} {page.source_refs?.length ? `· sources: ${page.source_refs.length}` : "· no source — review"}</p>}
          </div>
          <div>
            <h3 className="mb-1 text-sm font-semibold">Image quality checks</h3>
            {rev?.checks ? <ul className="space-y-1">{rev.checks.map((c) => <CheckRow key={c.id} c={c} />)}</ul> : <p className="text-sm text-slate-500">—</p>}
            {rev?.enhancement && <p className="mt-2 text-xs text-slate-500">Enhancement: {rev.enhancement.steps.join(" → ")}; detail preserved {Math.round(rev.enhancement.detail_preservation * 100)}%; vectorization {rev.enhancement.vectorization}.</p>}
            {rev?.text_detection && <p className="text-xs text-slate-500">Text/watermark detection: {rev.text_detection}</p>}
          </div>
        </section>

        <section>
          <h3 className="mb-2 text-sm font-semibold">Revision history</h3>
          <div className="flex gap-2 overflow-x-auto pb-2">
            {page.revisions?.filter((r) => r.image_url).map((r) => (
              <figure key={r.id} className={`w-28 shrink-0 rounded-lg border p-1 text-[10px] ${r.id === rev?.id ? "border-brand-500 ring-2 ring-brand-100" : "border-slate-200"}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={r.image_url!} alt="" className="aspect-[3/4] w-full bg-white object-contain" loading="lazy" />
                <figcaption className="mt-1">{r.kind} · {r.provider_id}<br />{new Date(r.created_at).toLocaleTimeString()}{r.status === "stale" ? " · stale" : ""}</figcaption>
                {r.id !== rev?.id && !page.locked && r.status !== "failed" && (
                  <button className="mt-1 w-full rounded bg-slate-100 px-1 py-0.5 hover:bg-slate-200" onClick={() => action(`/pages/${page.id}/restore`, { revision_id: r.id })}>Restore</button>
                )}
              </figure>
            ))}
          </div>
        </section>
        {rev?.prompt && <details className="text-xs text-slate-500"><summary>Prompt sent to provider</summary><p className="mt-1 whitespace-pre-wrap">{rev.prompt}</p></details>}
      </div>
    </div>
  );
}
