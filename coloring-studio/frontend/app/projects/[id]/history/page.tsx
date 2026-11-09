"use client";

import { useParams } from "next/navigation";
import { useEffect } from "react";
import { useProject } from "@/components/project-context";
import { Button, Card, ErrorNote } from "@/components/ui";
import { post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function History() {
  const { id } = useParams<{ id: string }>();
  const { tick, bump } = useProject();
  const { data, reload } = useResource<any[]>(`/projects/${id}/history`);
  const act = useAction();
  useEffect(() => { reload(); }, [tick, reload]);
  return (
    <Card title="Edit history" actions={<Button variant="secondary" disabled={act.busy} onClick={() => act.run(async () => { await post(`/projects/${id}/undo`); reload(); bump(); })}>Undo last change</Button>}>
      <ErrorNote error={act.error} />
      <ol className="divide-y divide-slate-100 text-sm">
        {data?.map((h) => (
          <li key={h.id} className={`flex flex-wrap gap-3 py-1.5 ${h.undone ? "text-slate-400 line-through" : ""}`}>
            <span className="w-40 text-xs text-slate-500">{new Date(h.created_at).toLocaleString()}</span>
            <span className="w-32 font-medium">{h.action.replace(/_/g, " ")}</span>
            <span className="w-20">{h.page_position ? `page ${h.page_position}` : "book"}</span>
            <span className="text-xs text-slate-500">via {h.source}</span>
            <span className="truncate text-xs text-slate-600">{Object.entries(h.details || {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ")}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}
