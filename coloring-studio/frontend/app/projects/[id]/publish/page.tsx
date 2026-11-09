"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import JobsBar from "@/components/JobsBar";
import { useProject } from "@/components/project-context";
import { Button, Card, CheckRow, ErrorNote } from "@/components/ui";
import { Check, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function Publish() {
  const { id } = useParams<{ id: string }>();
  const { project, tick, reload: reloadProject } = useProject();
  const qa = useResource<{ passed: boolean; checks: Check[]; interior_page_count: number }>(`/projects/${id}/qa?final=true`);
  const exports = useResource<any[]>(`/projects/${id}/exports`, 3000);
  const act = useAction();
  useEffect(() => { qa.reload(); exports.reload(); }, [tick]); // eslint-disable-line react-hooks/exhaustive-deps
  const fails = qa.data?.checks.filter((c) => c.status === "fail") || [];
  const [approved, setApproved] = useState(false);
  useEffect(() => { setApproved(!!project?.publication_approved); }, [project?.publication_approved]);

  return (
    <div className="space-y-4">
      <Card title="Publishing & Export" actions={<Button variant="secondary" onClick={qa.reload}>Re-run validation</Button>}>
        <ErrorNote error={act.error} />
        <p className="text-sm">{qa.data ? (qa.data.passed ? "✅ All mandatory checks pass. Ready for final export." : `⛔ ${fails.length} mandatory check(s) failing — final export is blocked until fixed.`) : "Validating…"}</p>
        <ul className="mt-3 space-y-1">{qa.data?.checks.map((c) => <CheckRow key={c.id} c={c} />)}</ul>
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={approved} onChange={(e) => {
              const v = e.target.checked;
              setApproved(v); // optimistic; reverted below if the request fails
              act.run(async () => {
                try {
                  await post(`/projects/${id}/publication-approval`, { approved: v });
                } catch (err) { setApproved(!v); throw err; }
                reloadProject(); qa.reload();
              });
            }} />
            I have reviewed the whole book and approve it for publication
          </label>
          <Button variant="secondary" disabled={act.busy} onClick={() => act.run(async () => { await post(`/projects/${id}/export`, { final: false, kinds: ["interior", "cover"] }); exports.reload(); })}>Build preview PDF</Button>
          <Button disabled={act.busy || !qa.data?.passed} onClick={() => act.run(async () => { await post(`/projects/${id}/export`, { final: true }); exports.reload(); })}>Export final KDP files</Button>
        </div>
        <p className="mt-2 text-xs text-slate-500">Local validation is a preflight check against KDP&apos;s published specifications — KDP&apos;s own review remains the final authority.</p>
      </Card>
      <JobsBar projectId={id} />
      <Card title="Exported files">
        {exports.data?.length ? (
          <ul className="divide-y divide-slate-100 text-sm">
            {exports.data.map((e) => (
              <li key={e.id} className="flex flex-wrap items-center gap-3 py-2">
                <span className="w-28 font-medium capitalize">{e.kind.replace("_", " ")}</span>
                <span className="text-xs text-slate-500">{new Date(e.created_at).toLocaleString()} · {(e.size / 1024).toFixed(0)} KB · sha256 {e.checksum.slice(0, 10)}…</span>
                {e.passed === false && <span className="text-xs text-rose-700">validation failed</span>}
                <a className="ml-auto text-brand-700 underline" href={e.download_url}>Download</a>
              </li>
            ))}
          </ul>
        ) : <p className="text-sm text-slate-500">No exports yet.</p>}
      </Card>
    </div>
  );
}
