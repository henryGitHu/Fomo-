"use client";

import Link from "next/link";
import { useState } from "react";
import { Button, Card, Empty, ErrorNote, Progress } from "@/components/ui";
import { Project, del, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function Dashboard() {
  const [showArchived, setShowArchived] = useState(false);
  const { data, error, reload } = useResource<Project[]>(`/projects?include_archived=${showArchived}`, 5000);
  const act = useAction();

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Publishing Dashboard</h1>
          <p className="text-sm text-slate-600">Your permanent library of coloring book projects. Everything is saved on the server automatically.</p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> Show archived
          </label>
          <Link href="/new" className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700">+ New book</Link>
        </div>
      </div>
      <ErrorNote error={error || act.error} />
      {data && data.length === 0 && <Empty>No books yet. Create your first coloring book to get started.</Empty>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {data?.map((p) => {
          const frac = p.progress.planned ? p.progress.approved / p.progress.planned : 0;
          return (
            <Card key={p.id} className="flex flex-col">
              <Link href={`/projects/${p.id}/studio`} className="group block" aria-label={`Open ${p.title}`}>
                <div className="checker mb-3 grid aspect-[3/4] place-items-center overflow-hidden rounded-xl border border-slate-200 bg-white">
                  {p.thumbnail_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={p.thumbnail_url} alt="" className="h-full w-full bg-white object-contain transition group-hover:scale-[1.02]" />
                  ) : (
                    <span className="text-4xl text-slate-300" aria-hidden>✎</span>
                  )}
                </div>
                <h2 className="font-semibold leading-tight group-hover:text-brand-700">{p.title}</h2>
              </Link>
              <p className="text-xs text-slate-500">{p.theme || "No theme"} · ages {p.audience_age} · {p.trim_size}"</p>
              <div className="mt-3 space-y-1 text-xs text-slate-600">
                <div className="flex justify-between"><span>{p.progress.generated}/{p.illustration_count} illustrated · {p.progress.approved} approved</span><span className="capitalize">{p.status}</span></div>
                <Progress value={frac} label={`${p.title} approval progress`} />
                {p.active_jobs > 0 && <p className="text-sky-700">⟳ {p.active_jobs} job(s) running</p>}
              </div>
              <div className="mt-auto flex flex-wrap gap-1 pt-3">
                <Link href={`/projects/${p.id}/studio`} className="rounded-lg px-2 py-1 text-sm font-medium text-brand-700 hover:bg-brand-50">Open</Link>
                <Button variant="ghost" disabled={act.busy} onClick={() => act.run(async () => { await post(`/projects/${p.id}/duplicate`); reload(); })}>Duplicate</Button>
                <Button variant="ghost" disabled={act.busy} onClick={() => act.run(async () => { await post(`/projects/${p.id}/archive?archived=${!p.archived}`); reload(); })}>{p.archived ? "Unarchive" : "Archive"}</Button>
                <Button variant="ghost" className="text-rose-700" disabled={act.busy} onClick={() => {
                  const t = window.prompt(`Type the title to permanently delete:\n${p.title}`);
                  if (t !== null) act.run(async () => { await del(`/projects/${p.id}?confirm_title=${encodeURIComponent(t)}`); reload(); });
                }}>Delete</Button>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
