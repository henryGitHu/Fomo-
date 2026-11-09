"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import JobsBar from "@/components/JobsBar";
import { useProject } from "@/components/project-context";
import { Button, Card, Empty, ErrorNote, Field, inputCls } from "@/components/ui";
import { post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function Research() {
  const { id } = useParams<{ id: string }>();
  const { project, tick } = useProject();
  const { data: rr, reload } = useResource<any>(`/projects/${id}/research`, 3000);
  const act = useAction();
  const [topic, setTopic] = useState("");
  useEffect(() => { reload(); }, [tick, reload]);
  useEffect(() => { if (project && !topic) setTopic(project.theme); }, [project]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-4">
      <Card title="Smart Theme Research" actions={
        <div className="flex items-end gap-2">
          <Field label="Topic"><input className={inputCls} value={topic} onChange={(e) => setTopic(e.target.value)} /></Field>
          <Button disabled={act.busy} onClick={() => act.run(async () => { await post(`/projects/${id}/research`, { topic }); reload(); })}>Run research</Button>
        </div>}>
        <p className="text-sm text-slate-600">Uses official APIs only (no scraping, paywalls or logins), curated knowledge packs and — if configured — a text model. Sources are inspiration for <strong>original</strong> artwork; nothing is copied.</p>
        <ErrorNote error={act.error} />
      </Card>
      <JobsBar projectId={id} />
      {!rr ? <Empty>No research yet.</Empty> : (
        <>
          <Card title={`Report: ${rr.topic}`} actions={<Link href={`/projects/${id}/plan`} className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white">Continue to Section Planner →</Link>}>
            <p className="mb-2 text-xs"><span className="rounded bg-slate-100 px-2 py-0.5">Mode: {rr.mode}</span> <span className="ml-2 rounded bg-slate-100 px-2 py-0.5">Web: {rr.web_status}</span></p>
            <p className="text-sm">{rr.overview}</p>
            <p className="mt-2 text-xs text-slate-500">{rr.originality_note}</p>
          </Card>
          <div className="grid gap-4 md:grid-cols-2">
            <Card title="Suggested sections & objectives">
              <ol className="list-decimal space-y-1 pl-5 text-sm">{rr.suggested_sections?.map((s: any) => <li key={s.name}><strong>{s.name}</strong> <span className="text-xs text-slate-500">({s.stage})</span> — {s.goals}</li>)}</ol>
            </Card>
            <Card title="Accuracy pitfalls to avoid">
              <ul className="list-disc space-y-1 pl-5 text-sm">{rr.pitfalls?.map((p: string) => <li key={p}>{p}</li>)}</ul>
              <h3 className="mt-3 text-sm font-semibold">Licensing notes</h3>
              <ul className="list-disc space-y-1 pl-5 text-xs text-slate-600">{rr.licensing_notes?.map((p: string) => <li key={p}>{p}</li>)}</ul>
            </Card>
          </div>
          <Card title={`Original illustration ideas (${rr.subjects?.length})`}>
            <ul className="grid gap-2 text-sm sm:grid-cols-2">
              {rr.subjects?.map((s: any, i: number) => (
                <li key={i} className="rounded-lg border border-slate-100 p-2">
                  <strong>{s.subject}</strong> <span className="text-xs text-slate-500">stage {s.stage + 1}{s.verified ? " · curated" : " · unverified"}</span>
                  <p className="text-xs text-slate-600">{s.scene}</p>
                  {s.fact && <p className="mt-1 text-xs">💡 {s.fact}</p>}
                </li>
              ))}
            </ul>
          </Card>
          <Card title="References">
            <ul className="space-y-1 text-sm">{rr.references?.map((r: any) => (
              <li key={r.id}><a className="text-brand-700 underline" href={r.url} target="_blank" rel="noreferrer">{r.title}</a> — {r.publisher} <span className="text-xs text-slate-500">({r.license})</span></li>
            ))}</ul>
          </Card>
        </>
      )}
    </div>
  );
}
