"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useProject } from "@/components/project-context";
import { Button, Card, ErrorNote, inputBase, inputCls } from "@/components/ui";
import { Page, patch, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

const STATUS: Record<string, string> = { ok: "✓ ok", review: "! review", invalid: "✕ fix", missing: "– missing" };

function Row({ p, onSaved }: { p: Page; onSaved: () => void }) {
  const [text, setText] = useState(p.caption_text);
  const act = useAction();
  const [notes, setNotes] = useState<string | null>(null);
  useEffect(() => setText(p.caption_text), [p.caption_version]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <tr className="border-t border-slate-100 align-top">
      <td className="py-2 pr-2 text-xs">
        {p.current_revision?.image_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={p.current_revision.image_url} alt="" className="h-24 w-20 rounded border bg-white object-contain" loading="lazy" />
        ) : <div className="grid h-24 w-20 place-items-center rounded border text-[10px] text-slate-400">no art</div>}
      </td>
      <td className="py-2 pr-2 text-sm"><strong>{p.position}.</strong> {p.subject}{p.locked && " 🔒"}<p className="mt-1 text-xs text-slate-500">{p.fact}</p></td>
      <td className="w-[45%] py-2 pr-2">
        <label className="sr-only" htmlFor={`cap-${p.id}`}>Caption for page {p.position}</label>
        <textarea id={`cap-${p.id}`} rows={3} className={inputCls} value={text} disabled={p.locked} onChange={(e) => setText(e.target.value)} />
        {notes && <p className="text-xs text-amber-800">{notes}</p>}
        <ErrorNote error={act.error} />
      </td>
      <td className="py-2 text-xs">
        <span className={p.caption_status === "ok" ? "text-emerald-700" : p.caption_status === "invalid" ? "text-rose-700" : "text-amber-700"}>{STATUS[p.caption_status] || p.caption_status}</span>
        <div className="mt-2 flex flex-col gap-1">
          <Button disabled={p.locked || text === p.caption_text || act.busy} onClick={() => act.run(async () => {
            const r = await patch(`/pages/${p.id}`, { caption_text: text });
            const bad = r.caption_validation?.checks?.filter((c: any) => c.status !== "pass").map((c: any) => c.message);
            setNotes(bad?.length ? bad.join("; ") : null);
            onSaved();
          })}>Save</Button>
          <Button variant="secondary" disabled={p.locked || act.busy} onClick={() => act.run(async () => { await post(`/pages/${p.id}/caption/regenerate`); onSaved(); })}>Regenerate</Button>
        </div>
      </td>
    </tr>
  );
}

export default function Captions() {
  const { id } = useParams<{ id: string }>();
  const { project, tick, reload: reloadProject } = useProject();
  const { data, reload } = useResource<Page[]>(`/projects/${id}/pages`);
  const act = useAction();
  const [filter, setFilter] = useState("all");
  useEffect(() => { reload(); }, [tick, reload]);
  const cs = (project?.caption_settings || {}) as Record<string, any>;
  const setSetting = (k: string, v: unknown) => act.run(async () => { await patch(`/projects/${id}`, { caption_settings: { ...cs, [k]: v } }); reloadProject(); });
  const rows = (data || []).filter((p) => filter === "all" || p.caption_status === filter);

  return (
    <div className="space-y-4">
      <Card title="Educational Caption Editor" actions={
        <Button variant="secondary" disabled={act.busy} onClick={() => act.run(async () => {
          if (window.confirm("Regenerate all unlocked captions from the planned facts? Hand-edited captions will be replaced (history is kept).")) {
            await post(`/projects/${id}/captions/regenerate`, {}); reload();
          }
        })}>Regenerate all</Button>}>
        <ErrorNote error={act.error} />
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={!!cs.enabled} onChange={(e) => setSetting("enabled", e.target.checked)} />Captions enabled</label>
          <label className="flex items-center gap-2">Reading level <select className={inputBase + " w-auto"} value={cs.reading_level || "intermediate"} onChange={(e) => setSetting("reading_level", e.target.value)}><option>early</option><option>intermediate</option><option>advanced</option></select></label>
          <label className="flex items-center gap-2">Length <select className={inputBase + " w-auto"} value={cs.length || "short"} onChange={(e) => setSetting("length", e.target.value)}><option>short</option><option>medium</option><option>long</option></select></label>
          <label className="flex items-center gap-2">Tone <select className={inputBase + " w-auto"} value={cs.tone || "cheerful"} onChange={(e) => setSetting("tone", e.target.value)}><option>cheerful</option><option>calm</option><option>curious</option></select></label>
          {["fun_fact", "vocabulary", "explanation", "question"].map((k) => (
            <label key={k} className="flex items-center gap-2"><input type="checkbox" checked={!!cs[k]} onChange={(e) => setSetting(k, e.target.checked)} />{k.replace("_", " ")}</label>
          ))}
          <label className="flex items-center gap-2">Font size <input type="number" min={10} max={20} className={inputBase + " w-20"} value={cs.font_size || 13} onChange={(e) => setSetting("font_size", Number(e.target.value))} /></label>
        </div>
        <p className="mt-2 text-xs text-slate-500">Each caption is checked for fit in the caption band (max {cs.max_lines || 3} lines at print size), reading level, relevance to the scene, safety, and whether its fact has a source. Approving a page marks its caption as human-reviewed.</p>
        <div className="mt-2 flex gap-1 text-xs">{["all", "ok", "review", "invalid", "missing"].map((f) => (
          <button key={f} onClick={() => setFilter(f)} className={`rounded-full px-2 py-0.5 ${filter === f ? "bg-ink text-white" : "bg-slate-100"}`}>{f} ({f === "all" ? data?.length || 0 : (data || []).filter((p) => p.caption_status === f).length})</button>
        ))}</div>
      </Card>
      <Card>
        <div className="overflow-x-auto">
          <table className="w-full"><thead className="text-left text-xs text-slate-500"><tr><th>Art</th><th>Page</th><th>Caption</th><th>Status</th></tr></thead>
            <tbody>{rows.map((p) => <Row key={p.id} p={p} onSaved={reload} />)}</tbody></table>
        </div>
      </Card>
    </div>
  );
}
