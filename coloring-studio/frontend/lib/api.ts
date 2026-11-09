// Typed client for the FastAPI backend (proxied at /api by next.config.mjs).

export type Check = { id: string; status: "pass" | "warn" | "fail"; mandatory: boolean; message: string; page?: number | null; fix?: string | null };

export type Revision = {
  id: string; kind: string; provider_id: string; model_id: string; image_url: string | null;
  width: number | null; height: number | null; cost: number | null; cost_is_estimate: boolean; status: string;
  created_at: string; qa_passed: boolean | null; scores: Record<string, number>; effective_ppi: number | null;
  native_ppi: number | null; battle_id: string | null; checks?: Check[]; prompt?: string;
  enhancement?: { steps: string[]; detail_preservation: number; upscale_factor: number; vectorization: string } | null;
  metrics?: Record<string, number>; duration_seconds?: number | null; error?: string | null;
  effective_params?: Record<string, unknown>; text_detection?: string;
};

export type Page = {
  id: string; position: number; section_id: string | null; subject: string; concept: string; fact: string;
  vocabulary: string; question: string; status: string; locked: boolean; caption_text: string;
  caption_status: string; caption_version: number; provider_override: string | null; model_override: string | null;
  version: number; source_refs: string[]; current_revision: Revision | null;
  revisions?: Revision[]; caption_history?: { id: string; text: string; source: string; created_at: string }[];
  caption_validation?: { lines: string[]; grade: number; checks: Check[]; passed: boolean } | null;
};

export type Section = {
  id: string; project_id: string; position: number; name: string; illustration_count: number;
  theme_instructions: string; desired_scenes: string[]; objects: string[]; complexity: string;
  educational_goals: string; caption_preferences: Record<string, unknown>; excluded_content: string;
  preferred_provider: string | null; preferred_model: string | null; learning_stage: string; locked: boolean;
  version: number; actual_count?: number; page_positions?: number[]; approved?: number;
};

export type Project = {
  id: string; title: string; subtitle: string; author: string; theme: string; audience_age: string;
  illustration_count: number; trim_size: string; paper_type: string; bleed: boolean; illustration_style: string;
  caption_settings: Record<string, unknown>; budget_limit: number | null; generation_mode: string;
  routing_preference: string; default_provider: string; default_model: string; max_retries: number;
  fallback_enabled: boolean; enhance_images: boolean; batch_size: number; autopilot: boolean;
  education_progression: boolean; intro_pages: number; back_description: string; publication_approved: boolean;
  status: string; archived: boolean; version: number; created_at: string; updated_at: string;
  progress: { planned: number; generated: number; approved: number; by_status: Record<string, number> };
  thumbnail_url: string | null; interior_page_count: number; active_jobs: number;
  sections?: Section[]; geometry?: Record<string, number | boolean>; art_box_in?: [number, number];
  target_px?: [number, number]; cover_geometry?: Record<string, unknown>;
};

export type Job = {
  id: string; project_id: string | null; type: string; status: string; payload: Record<string, unknown>;
  result: Record<string, any>; progress: number; progress_message: string; error: string | null;
  attempts: number; created_at: string; started_at: string | null; finished_at: string | null;
};

export type ModelCaps = {
  model_id: string; display_name: string; image_edit: boolean; mask_edit: boolean; reference_images: boolean;
  negative_prompt: boolean; seed: boolean; aspect_ratios: string[]; max_side: number; price_per_image: number | null;
  pricing_note: string; async_jobs: boolean; measured_avg_quality: number | null; measured_avg_latency: number | null;
  measured_failure_rate: number | null; samples: number; quality_levels: string[];
};

export type Provider = {
  provider_id: string; display_name: string; kind: "image" | "text"; docs_url?: string; is_mock: boolean;
  enabled: boolean; priority?: number; default_model?: string | null; key_source: string; key_masked: string | null;
  requires_api_key?: boolean; status?: string; usable?: boolean; last_test_at?: string | null;
  last_test_ok?: boolean | null; last_test_message?: string | null; last_success_at?: string | null;
  settings?: Record<string, unknown>; models: ModelCaps[];
};

export type ChatMsg = { id: string; role: "user" | "assistant"; content: string; actions: any[]; created_at: string };

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as any)?.message || JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

export async function api<T = any>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  let body = init?.body;
  if (init?.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const token = typeof window !== "undefined" ? window.localStorage?.getItem("studioToken") : null;
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, { ...init, headers, body, cache: "no-store" });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail;
    } catch {}
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const post = <T = any>(path: string, json: unknown = {}) => api<T>(path, { method: "POST", json });
export const patch = <T = any>(path: string, json: unknown) => api<T>(path, { method: "PATCH", json });
export const put = <T = any>(path: string, json: unknown) => api<T>(path, { method: "PUT", json });
export const del = <T = any>(path: string) => api<T>(path, { method: "DELETE" });

export const STATUS_LABEL: Record<string, string> = {
  planned: "Planned", queued: "Queued", generating: "Generating", generated: "Generated",
  awaiting_review: "Awaiting review", revision_requested: "Revision requested", approved: "Approved",
  locked: "Locked", export_ready: "Export ready", failed: "Failed",
};

export const STATUS_COLOR: Record<string, string> = {
  planned: "bg-slate-100 text-slate-700", queued: "bg-sky-100 text-sky-800", generating: "bg-sky-200 text-sky-900",
  generated: "bg-indigo-100 text-indigo-800", awaiting_review: "bg-amber-100 text-amber-900",
  revision_requested: "bg-orange-100 text-orange-900", approved: "bg-emerald-100 text-emerald-800",
  locked: "bg-emerald-700 text-white", export_ready: "bg-emerald-800 text-white", failed: "bg-rose-100 text-rose-800",
};

export function money(v: number | null | undefined, unknownLabel = "unknown") {
  if (v === null || v === undefined) return unknownLabel;
  return `$${v.toFixed(v < 1 ? 3 : 2)}`;
}
