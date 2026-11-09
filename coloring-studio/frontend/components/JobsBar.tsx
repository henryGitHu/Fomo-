"use client";

import { useEffect, useRef } from "react";
import { Job, post } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { Button, Progress } from "./ui";
import { useProject } from "./project-context";

/** Active/recent jobs for a project with pause / resume / cancel controls. */
export default function JobsBar({ projectId }: { projectId: string }) {
  const { bump, tick } = useProject();
  const { data, reload } = useResource<Job[]>(`/jobs?project_id=${projectId}&limit=8`, 1500);
  const prev = useRef<string>("");
  useEffect(() => { reload(); }, [tick, reload]);
  useEffect(() => {
    const sig = (data || []).map((j) => `${j.id}:${j.status}:${(j.result?.done || []).length}`).join("|");
    if (prev.current && sig !== prev.current) bump();
    prev.current = sig;
  }, [data, bump]);
  const visible = (data || []).filter((j) => ["queued", "running", "paused"].includes(j.status) ||
    (j.finished_at && Date.now() - new Date(j.finished_at).getTime() < 20000));
  if (!visible.length) return null;
  return (
    <div className="space-y-2" aria-label="Background jobs">
      {visible.map((j) => (
        <div key={j.id} className="rounded-xl border border-slate-200 bg-white p-2 text-sm" role="status">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <span className="font-medium">{j.type.replace("_", " ")}</span>
            <span className="text-xs text-slate-500">{j.status}{j.progress_message ? ` — ${j.progress_message}` : ""}</span>
            {j.result?.stopped_reason && <span className="text-xs text-rose-700">Stopped: {j.result.stopped_reason}</span>}
            {j.error && <span className="text-xs text-rose-700">{j.error}</span>}
            <span className="ml-auto flex gap-1">
              {j.status === "running" && <Button variant="ghost" onClick={async () => { await post(`/jobs/${j.id}/pause`); reload(); }}>Pause</Button>}
              {["paused", "failed"].includes(j.status) && <Button variant="ghost" onClick={async () => { await post(`/jobs/${j.id}/resume`); reload(); }}>Resume</Button>}
              {["queued", "running", "paused"].includes(j.status) && <Button variant="ghost" onClick={async () => { await post(`/jobs/${j.id}/cancel`); reload(); }}>Cancel</Button>}
            </span>
          </div>
          <Progress value={j.status === "succeeded" ? 1 : j.progress} label={`${j.type} progress`} />
          {(j.result?.outcomes || []).filter((o: any) => o.status !== "ok").slice(-3).map((o: any, i: number) => (
            <p key={i} className="mt-1 text-xs text-amber-800">Page {o.position ?? "?"}: {o.status} — {o.message}</p>
          ))}
        </div>
      ))}
    </div>
  );
}
