"use client";

import { createContext, useContext } from "react";
import { Project } from "@/lib/api";

export type ProjectCtx = {
  project: Project | null;
  reload: () => void;
  /** Incremented whenever something changed the book (chat edit, job finished) so screens can refetch. */
  tick: number;
  bump: () => void;
};

export const ProjectContext = createContext<ProjectCtx>({ project: null, reload: () => {}, tick: 0, bump: () => {} });
export const useProject = () => useContext(ProjectContext);
