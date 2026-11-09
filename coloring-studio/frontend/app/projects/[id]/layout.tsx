"use client";

import { useParams, usePathname } from "next/navigation";
import { ReactNode, useCallback, useEffect, useState } from "react";
import ChatPanel from "@/components/ChatPanel";
import { ProjectContext } from "@/components/project-context";
import { ErrorNote, NavLink } from "@/components/ui";
import { Project, api } from "@/lib/api";

const TABS = [
  ["research", "Smart Research"], ["plan", "Section Planner"], ["studio", "Illustration Studio"], ["battle", "AI Model Battle"],
  ["captions", "Captions"], ["preview", "Book Preview"], ["publish", "Publish & Export"], ["costs", "Costs"],
  ["history", "History"], ["settings", "Settings"],
] as const;

export default function ProjectLayout({ children }: { children: ReactNode }) {
  const { id } = useParams<{ id: string }>();
  const path = usePathname();
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((t) => t + 1), []);
  const reload = useCallback(() => {
    api<Project>(`/projects/${id}`).then((p) => { setProject(p); setError(null); }).catch((e) => setError(e.message));
  }, [id]);
  useEffect(() => { reload(); }, [reload, tick]);
  useEffect(() => {
    const t = setInterval(reload, 6000);
    return () => clearInterval(t);
  }, [reload]);

  return (
    <ProjectContext.Provider value={{ project, reload, tick, bump }}>
      <div className="mb-4 flex flex-wrap items-baseline gap-x-3">
        <h1 className="text-xl font-bold">{project?.title || "…"}</h1>
        {project && <span className="text-sm text-slate-500">{project.illustration_count} illustrations · {project.interior_page_count} interior pages · {project.progress.approved} approved{project.active_jobs ? ` · ⟳ ${project.active_jobs} job(s)` : ""}</span>}
      </div>
      <nav className="mb-4 flex gap-1 overflow-x-auto pb-1" aria-label="Book workflow">
        {TABS.map(([slug, label]) => <NavLink key={slug} href={`/projects/${id}/${slug}`} active={path.endsWith(`/${slug}`)}>{label}</NavLink>)}
      </nav>
      <ErrorNote error={error} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0">{children}</div>
        <ChatPanel projectId={id} />
      </div>
    </ProjectContext.Provider>
  );
}
