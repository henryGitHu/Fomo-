"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ChatMsg, Job, api, post } from "@/lib/api";
import { useProject } from "./project-context";

const EXAMPLES = [
  "Generate the next five illustrations.",
  "Make the lines thicker on pages 6–12.",
  "Rewrite the caption on page 9.",
  "Try page 3 in mock and atelier and compare the results.",
  "Keep all approved pages unchanged.",
  "undo",
];

/** Live Book Editor — docked, collapsible; lives in the project layout so it persists across screens. */
export default function ChatPanel({ projectId }: { projectId: string }) {
  const { bump } = useProject();
  const [open, setOpen] = useState(true);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      const v = window.localStorage.getItem("chatOpen");
      if (v !== null) setOpen(v === "1");
    } catch {}
    api<ChatMsg[]>(`/projects/${projectId}/chat`).then(setMsgs).catch(() => {});
  }, [projectId]);

  useEffect(() => {
    try { window.localStorage.setItem("chatOpen", open ? "1" : "0"); } catch {}
  }, [open]);

  useEffect(() => {
    // newer browsers return a Promise from scrollIntoView; effects must not return it
    endRef.current?.scrollIntoView({ block: "end" });
  }, [msgs, open]);

  // Track jobs started from chat; refresh the book when they finish.
  useEffect(() => {
    if (!jobs.some((j) => ["queued", "running"].includes(j.status))) return;
    const t = setInterval(async () => {
      const fresh = await Promise.all(jobs.map((j) => api<Job>(`/jobs/${j.id}`).catch(() => j)));
      if (fresh.some((j, i) => j.status !== jobs[i].status || j.progress !== jobs[i].progress)) bump();
      setJobs(fresh);
    }, 1500);
    return () => clearInterval(t);
  }, [jobs, bump]);

  async function send(e?: FormEvent, override?: string) {
    e?.preventDefault();
    const message = (override ?? text).trim();
    if (!message) return;
    setBusy(true);
    setText("");
    setMsgs((m) => [...m, { id: `tmp-${Date.now()}`, role: "user", content: message, actions: [], created_at: new Date().toISOString() }]);
    try {
      const reply = await post<ChatMsg>(`/projects/${projectId}/chat`, { message });
      const all = await api<ChatMsg[]>(`/projects/${projectId}/chat`);
      setMsgs(all.length ? all : (m) => [...m, reply]);
      const newJobs = (reply.actions || []).filter((a: any) => a.job_id).map((a: any) => a.job_id as string);
      if (newJobs.length) setJobs(await Promise.all(newJobs.map((id: string) => api<Job>(`/jobs/${id}`))));
      bump();
    } catch (err: any) {
      setMsgs((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `⚠️ ${err.message}`, actions: [], created_at: "" }]);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} aria-expanded={false} aria-controls="live-editor"
        className="fixed bottom-4 right-4 z-40 rounded-full bg-brand-600 px-4 py-3 text-sm font-semibold text-white shadow-lg hover:bg-brand-700">
        💬 Live Book Editor
      </button>
    );
  }
  return (
    <aside id="live-editor" aria-label="Live Book Editor"
      className="flex h-[calc(100vh-7rem)] flex-col rounded-2xl border border-slate-200 bg-white shadow-sm lg:sticky lg:top-20">
      <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <h2 className="text-sm font-semibold">💬 Live Book Editor</h2>
        <button onClick={() => setOpen(false)} aria-expanded={true} className="rounded px-2 text-slate-500 hover:bg-slate-100" title="Collapse">–</button>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto px-3 py-2 text-sm" aria-live="polite">
        {msgs.length === 0 && (
          <div className="space-y-1 text-xs text-slate-500">
            <p>Tell me what to change. Edits apply to the saved book; locked pages are never touched.</p>
            {EXAMPLES.map((ex) => (
              <button key={ex} onClick={() => send(undefined, ex)} className="block w-full rounded-lg border border-slate-200 px-2 py-1 text-left hover:bg-slate-50">{ex}</button>
            ))}
          </div>
        )}
        {msgs.map((m) => (
          <div key={m.id} className={m.role === "user" ? "ml-6 rounded-xl bg-brand-50 px-3 py-2" : "mr-2 whitespace-pre-wrap rounded-xl bg-slate-50 px-3 py-2"}>
            {m.content}
            {m.actions?.filter((a: any) => a.type === "pending_confirmation").length > 0 && (
              <div className="mt-1 flex gap-2">
                <button className="rounded bg-brand-600 px-2 py-0.5 text-xs text-white" onClick={() => send(undefined, "confirm")}>Confirm</button>
                <button className="rounded border px-2 py-0.5 text-xs" onClick={() => send(undefined, "cancel")}>Cancel</button>
              </div>
            )}
          </div>
        ))}
        {jobs.map((j) => (
          <div key={j.id} className="rounded-lg border border-sky-100 bg-sky-50 px-2 py-1 text-xs text-sky-900" role="status">
            {j.type.replace("_", " ")}: {j.status} {j.status === "running" ? `${Math.round(j.progress * 100)}% ${j.progress_message}` : ""}
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <form onSubmit={send} className="flex gap-2 border-t border-slate-100 p-2">
        <label htmlFor="chat-input" className="sr-only">Message the Live Book Editor</label>
        <input id="chat-input" value={text} onChange={(e) => setText(e.target.value)} disabled={busy}
          placeholder="e.g. Regenerate page 14 using OpenAI"
          className="flex-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none" />
        <button type="submit" disabled={busy || !text.trim()} className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">Send</button>
      </form>
    </aside>
  );
}
