"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import JobsBar from "@/components/JobsBar";
import { useProject } from "@/components/project-context";
import { Button, Card, Empty, ErrorNote, Field, inputCls } from "@/components/ui";
import { Page, Provider, money, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function Battle() {
  const { id } = useParams<{ id: string }>();
  const { tick, bump } = useProject();
  const pages = useResource<Page[]>(`/projects/${id}/pages`);
  const providers = useResource<Provider[]>("/providers");
  const battles = useResource<any[]>(`/projects/${id}/battles`, 2500);
  const act = useAction();
  const [pageId, setPageId] = useState("");
  const [contenders, setContenders] = useState<{ provider_id: string; model_id: string }[]>([]);
  useEffect(() => { battles.reload(); }, [tick]); // eslint-disable-line react-hooks/exhaustive-deps
  const usable = (providers.data || []).filter((p) => p.kind === "image" && p.usable);
  const options = usable.flatMap((p) => p.models.map((m) => ({ provider_id: p.provider_id, model_id: m.model_id, label: `${p.display_name} — ${m.display_name}`, price: m.price_per_image })));
  const key = (c: { provider_id: string; model_id: string }) => `${c.provider_id}|${c.model_id}`;

  return (
    <div className="space-y-4">
      <Card title="AI Image Model Battle">
        <p className="mb-3 text-sm text-slate-600">Generate the same page concept with several models and compare side by side. Candidates never replace the page&apos;s artwork until you pick a winner; approved pages need an extra confirmation and locked pages can&apos;t be replaced.</p>
        <ErrorNote error={act.error} />
        <div className="grid gap-3 md:grid-cols-[1fr_2fr_auto] md:items-end">
          <Field label="Page">
            <select className={inputCls} value={pageId} onChange={(e) => setPageId(e.target.value)}>
              <option value="">Choose a page…</option>
              {pages.data?.map((p) => <option key={p.id} value={p.id}>{p.position}. {p.subject}{p.locked ? " 🔒" : ""}</option>)}
            </select>
          </Field>
          <fieldset className="text-sm">
            <legend className="mb-1 font-medium">Contenders (pick 2+)</legend>
            <div className="flex flex-wrap gap-2">
              {options.map((o) => (
                <label key={key(o)} className="flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1">
                  <input type="checkbox" checked={contenders.some((c) => key(c) === key(o))}
                    onChange={(e) => setContenders((cs) => e.target.checked ? [...cs, { provider_id: o.provider_id, model_id: o.model_id }] : cs.filter((c) => key(c) !== key(o)))} />
                  {o.label} <span className="text-xs text-slate-500">{money(o.price)}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <Button disabled={!pageId || contenders.length < 2 || act.busy} onClick={() => act.run(async () => {
            await post(`/projects/${id}/battles`, { page_id: pageId, contenders }); battles.reload();
          })}>Start battle</Button>
        </div>
      </Card>
      <JobsBar projectId={id} />
      {battles.data?.length === 0 && <Empty>No battles yet.</Empty>}
      {battles.data?.map((b) => (
        <Card key={b.id} title={`Page ${b.page_position}: ${b.page_subject}`} actions={<span className="text-xs text-slate-500">{b.status.replace("_", " ")} · {new Date(b.created_at).toLocaleString()}</span>}>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {b.candidates.map((c: any) => (
              <figure key={c.revision_id} className={`rounded-xl border p-2 ${c.is_winner ? "border-emerald-500 ring-2 ring-emerald-100" : "border-slate-200"}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={c.image_url} alt={`Candidate from ${c.provider_id}`} className="aspect-[3/4] w-full rounded-lg bg-white object-contain" />
                <figcaption className="mt-2 space-y-1 text-xs">
                  <p className="text-sm font-semibold">{c.provider_id} / {c.model_id} {c.is_winner && "🏆"}</p>
                  <p>{c.width}×{c.height}px · {c.effective_ppi} PPI{c.native_ppi ? ` (native ${c.native_ppi})` : ""} · {money(c.cost)}{c.cost_is_estimate ? " est." : ""} · {c.duration_seconds?.toFixed(2)}s</p>
                  <p className="font-medium">Objective scores (measured from pixels):</p>
                  <ul className="grid grid-cols-2 gap-x-2">
                    {Object.entries(c.objective_scores).map(([k, v]) => <li key={k}>{k.replace(/_/g, " ")}: <strong>{String(v)}</strong></li>)}
                  </ul>
                  <p className="text-slate-500">Subjective AI evaluation: {c.subjective_evaluation}</p>
                  <p>QA: {c.qa_passed ? "✓ passed" : "✕ issues"}</p>
                  {!c.is_winner && (
                    <Button onClick={() => act.run(async () => {
                      try {
                        await post(`/battles/${b.id}/select`, { revision_id: c.revision_id });
                      } catch (e: any) {
                        if (e.status === 409 && window.confirm("This page is approved. Replace its artwork with this candidate?")) {
                          await post(`/battles/${b.id}/select`, { revision_id: c.revision_id, confirm_replace_approved: true });
                        } else throw e;
                      }
                      battles.reload(); bump();
                    })}>Choose as winner</Button>
                  )}
                </figcaption>
              </figure>
            ))}
            {b.candidates.length === 0 && <p className="text-sm text-slate-500">Generating candidates…</p>}
          </div>
        </Card>
      ))}
    </div>
  );
}
