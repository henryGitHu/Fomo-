"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useProject } from "@/components/project-context";
import { Button, Card } from "@/components/ui";
import { useResource } from "@/lib/hooks";

type LayoutPage = { page: number; kind: string; position?: number; subject?: string; caption?: string; image_url?: string | null; status?: string };

function PhysicalPage({ p, project, geo, box }: { p: LayoutPage; project: any; geo: any; box: [number, number] }) {
  const ratio = `${geo.page_w} / ${geo.page_h}`;
  const pct = (inch: number, total: number) => `${(inch / total) * 100}%`;
  const recto = p.page % 2 === 1;
  const left = recto ? geo.inside : geo.outside;
  return (
    <div className="relative bg-white shadow ring-1 ring-slate-200" style={{ aspectRatio: ratio }} aria-label={`Interior page ${p.page}`}>
      {/* margin guides */}
      <div className="pointer-events-none absolute border border-dashed border-sky-200" style={{ left: pct(left, geo.page_w), right: pct(recto ? geo.outside : geo.inside, geo.page_w), top: pct(geo.top, geo.page_h), bottom: pct(geo.bottom, geo.page_h) }} />
      {p.kind === "illustration" && (
        <>
          <div className="absolute grid place-items-center" style={{ left: pct(left, geo.page_w), width: pct(box[0], geo.page_w), top: pct(geo.top, geo.page_h), height: pct(box[1], geo.page_h) }}>
            {p.image_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={p.image_url} alt={p.subject} className="max-h-full max-w-full object-contain" loading="lazy" />
            ) : <span className="text-[10px] text-slate-400">[{p.subject}]</span>}
          </div>
          <p className="absolute text-center leading-tight text-ink" style={{ left: pct(left, geo.page_w), width: pct(box[0], geo.page_w), bottom: pct(geo.bottom, geo.page_h), height: pct(1.1, geo.page_h), fontSize: "clamp(5px, 0.9vw, 11px)" }}>{p.caption}</p>
        </>
      )}
      {p.kind === "title" && <div className="absolute inset-0 grid place-items-center p-4 text-center"><div><p className="font-bold" style={{ fontSize: "clamp(8px,1.6vw,22px)" }}>{project?.title}</p><p className="text-slate-600" style={{ fontSize: "clamp(6px,1vw,14px)" }}>{project?.subtitle}</p></div></div>}
      {p.kind === "copyright" && <p className="absolute bottom-[30%] left-[10%] right-[10%] text-[7px] text-slate-500">© {project?.author} — copyright & AI-assistance disclosure</p>}
      {p.kind === "belongs_to" && <p className="absolute inset-x-0 top-[30%] text-center font-semibold" style={{ fontSize: "clamp(7px,1.2vw,16px)" }}>This book belongs to ________</p>}
      {p.kind === "contents" && <p className="absolute left-[10%] top-[10%] font-semibold" style={{ fontSize: "clamp(7px,1.2vw,16px)" }}>Contents</p>}
      <span className="absolute bottom-1 right-2 text-[9px] text-slate-400">{p.page}{p.kind === "blank_verso" ? " · blank back" : ""}</span>
    </div>
  );
}

export default function Preview() {
  const { id } = useParams<{ id: string }>();
  const { project, tick } = useProject();
  const { data, reload } = useResource<any>(`/projects/${id}/layout`);
  const [spread, setSpread] = useState(0);
  useEffect(() => { reload(); }, [tick, reload]);
  if (!data) return <p>Loading…</p>;
  const pages: LayoutPage[] = data.pages;
  // spreads: [1], [2,3], [4,5], ...
  const spreads: LayoutPage[][] = [[pages[0]]];
  for (let i = 1; i < pages.length; i += 2) spreads.push(pages.slice(i, i + 2));
  const cur = spreads[Math.min(spread, spreads.length - 1)];
  const c = data.cover;

  return (
    <div className="space-y-4">
      <Card title="Book Preview" actions={<span className="text-sm text-slate-600">{data.total} interior pages · trim {data.geometry.trim_w}×{data.geometry.trim_h}in{data.geometry.bleed ? " + bleed" : ""}</span>}>
        <div className="mb-3 flex items-center gap-2">
          <Button variant="secondary" disabled={spread === 0} onClick={() => setSpread((s) => s - 1)}>← Prev</Button>
          <span className="text-sm">Spread {spread + 1} / {spreads.length} (pages {cur.map((p) => p.page).join("–")})</span>
          <Button variant="secondary" disabled={spread >= spreads.length - 1} onClick={() => setSpread((s) => s + 1)}>Next →</Button>
          <input type="range" min={0} max={spreads.length - 1} value={spread} onChange={(e) => setSpread(Number(e.target.value))} className="ml-2 flex-1" aria-label="Jump to spread" />
        </div>
        <div className="mx-auto grid max-w-4xl grid-cols-2 gap-0 bg-slate-200 p-4">
          {cur.length === 1 ? <><div /><PhysicalPage p={cur[0]} project={project} geo={data.geometry} box={data.art_box_in} /></> :
            cur.map((p) => <PhysicalPage key={p.page} p={p} project={project} geo={data.geometry} box={data.art_box_in} />)}
        </div>
        <p className="mt-2 text-xs text-slate-500">Dashed lines show the safe margins (gutter {data.geometry.inside}in inside, {data.geometry.outside}in outside). Every illustration sits on a right-hand page with a blank back to prevent marker bleed-through.</p>
      </Card>
      <Card title="Full-wrap cover geometry">
        <div className="mx-auto flex max-w-4xl overflow-hidden rounded ring-1 ring-slate-300" style={{ aspectRatio: `${c.full_w} / ${c.full_h}` }}>
          <div className="grid flex-1 place-items-center bg-brand-700 text-xs text-white" style={{ flexBasis: `${(c.trim_w + 0.125) / c.full_w * 100}%` }}>Back cover<br />(barcode area {c.barcode_box[2]}×{c.barcode_box[3]}in bottom-right)</div>
          <div className="grid place-items-center bg-brand-600 text-[9px] text-white [writing-mode:vertical-rl]" style={{ flexBasis: `${c.spine_w / c.full_w * 100}%` }}>{c.spine_text_allowed ? "spine text" : ""}</div>
          <div className="grid flex-1 place-items-center bg-brand-500 text-xs text-white" style={{ flexBasis: `${(c.trim_w + 0.125) / c.full_w * 100}%` }}>Front cover</div>
        </div>
        <p className="mt-2 text-sm text-slate-600">Full cover {c.full_w}×{c.full_h}in · spine {c.spine_w}in for {c.page_count} {c.paper_type} pages {c.spine_text_allowed ? "(spine text allowed)" : "(too thin for spine text)"}.</p>
      </Card>
    </div>
  );
}
