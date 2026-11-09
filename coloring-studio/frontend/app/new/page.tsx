"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { Button, Card, ErrorNote, Field, inputBase, inputCls } from "@/components/ui";
import { Provider, post } from "@/lib/api";
import { useAction, useResource } from "@/lib/hooks";

export default function NewBook() {
  const router = useRouter();
  const providers = useResource<Provider[]>("/providers");
  const act = useAction();
  const [f, setF] = useState({
    title: "", subtitle: "", author: "", theme: "", audience_age: "6-9", illustration_count: 40, trim_size: "8.5x11",
    paper_type: "white", illustration_style: "clean cartoon line art", budget: "", default_provider: "mock",
    default_model: "", generation_mode: "manual", caption_enabled: true, reading_length: "short", fun_fact: true,
    vocabulary: false, question: false, education_progression: true,
  });
  const set = (k: string, v: unknown) => setF((s) => ({ ...s, [k]: v }));
  const imageProviders = (providers.data || []).filter((p) => p.kind === "image" && p.usable);
  const models = imageProviders.find((p) => p.provider_id === f.default_provider)?.models || [];

  async function submit(e: FormEvent) {
    e.preventDefault();
    const proj = await act.run(() => post("/projects", {
      title: f.title, subtitle: f.subtitle, author: f.author, theme: f.theme, audience_age: f.audience_age,
      illustration_count: Number(f.illustration_count), trim_size: f.trim_size, paper_type: f.paper_type,
      illustration_style: f.illustration_style, budget_limit: f.budget === "" ? null : Number(f.budget),
      default_provider: f.default_provider, default_model: f.default_model || models[0]?.model_id,
      generation_mode: f.generation_mode, education_progression: f.education_progression,
      caption_settings: { enabled: f.caption_enabled, length: f.reading_length, fun_fact: f.fun_fact,
        vocabulary: f.vocabulary, question: f.question },
    }));
    if (proj) {
      await post(`/projects/${proj.id}/research`, {});
      router.push(`/projects/${proj.id}/research`);
    }
  }

  return (
    <form onSubmit={submit} className="mx-auto max-w-4xl space-y-4" aria-label="New book setup">
      <h1 className="text-2xl font-bold">New Book Setup</h1>
      <ErrorNote error={act.error} />
      <Card title="Book">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Title"><input name="title" required className={inputCls} value={f.title} onChange={(e) => set("title", e.target.value)} placeholder="Little Chemical Engineers" /></Field>
          <Field label="Subtitle"><input name="subtitle" className={inputCls} value={f.subtitle} onChange={(e) => set("subtitle", e.target.value)} placeholder="A Learn-While-You-Color Adventure" /></Field>
          <Field label="Author / publisher"><input name="author" className={inputCls} value={f.author} onChange={(e) => set("author", e.target.value)} /></Field>
          <Field label="Theme" hint="Used for Smart Theme Research"><input name="theme" required className={inputCls} value={f.theme} onChange={(e) => set("theme", e.target.value)} placeholder="chemical engineering" /></Field>
          <Field label="Target age">
            <select className={inputCls} value={f.audience_age} onChange={(e) => set("audience_age", e.target.value)}>
              {["3-5", "4-8", "6-9", "8-12", "10-14", "adult"].map((a) => <option key={a}>{a}</option>)}
            </select>
          </Field>
          <Field label="Number of illustrations" hint={`Interior: ${4 + 2 * Number(f.illustration_count || 0)} pages (single-sided art + blank backs)`}>
            <input name="illustration_count" type="number" min={1} max={400} className={inputCls} value={f.illustration_count} onChange={(e) => set("illustration_count", e.target.value)} />
          </Field>
          <Field label="Trim size">
            <select className={inputCls} value={f.trim_size} onChange={(e) => set("trim_size", e.target.value)}>
              {["8.5x11", "8.25x11", "8x10", "8.5x8.5", "8.25x8.25", "7x10", "6x9"].map((t) => <option key={t}>{t}</option>)}
            </select>
          </Field>
          <Field label="Paper"><select className={inputCls} value={f.paper_type} onChange={(e) => set("paper_type", e.target.value)}><option value="white">White</option><option value="cream">Cream</option></select></Field>
          <Field label="Illustration style"><input className={inputCls} value={f.illustration_style} onChange={(e) => set("illustration_style", e.target.value)} /></Field>
          <label className="flex items-center gap-2 self-end text-sm"><input type="checkbox" checked={f.education_progression} onChange={(e) => set("education_progression", e.target.checked)} /> Educational learning progression</label>
        </div>
      </Card>
      <Card title="Captions — Learn While You Color">
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.caption_enabled} onChange={(e) => set("caption_enabled", e.target.checked)} /> Captions enabled</label>
          <label className="flex items-center gap-2">Length <select className={inputBase + " w-auto"} value={f.reading_length} onChange={(e) => set("reading_length", e.target.value)}><option>short</option><option>medium</option><option>long</option></select></label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.fun_fact} onChange={(e) => set("fun_fact", e.target.checked)} /> Fun fact</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.vocabulary} onChange={(e) => set("vocabulary", e.target.checked)} /> Vocabulary word</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.question} onChange={(e) => set("question", e.target.checked)} /> Question</label>
        </div>
      </Card>
      <Card title="Generation & budget">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Default image provider" hint="Only configured providers are listed. Mock providers are offline simulations.">
            <select className={inputCls} value={f.default_provider} onChange={(e) => { set("default_provider", e.target.value); set("default_model", ""); }}>
              {imageProviders.map((p) => <option key={p.provider_id} value={p.provider_id}>{p.display_name}</option>)}
            </select>
          </Field>
          <Field label="Model">
            <select className={inputCls} value={f.default_model} onChange={(e) => set("default_model", e.target.value)}>
              {models.map((m) => <option key={m.model_id} value={m.model_id}>{m.display_name}</option>)}
            </select>
          </Field>
          <Field label="Generation mode">
            <select className={inputCls} value={f.generation_mode} onChange={(e) => set("generation_mode", e.target.value)}>
              <option value="manual">Manual selection</option><option value="auto">Smart auto-router</option><option value="quality_first">Quality-first</option>
            </select>
          </Field>
          <Field label="Spending limit (USD)" hint="Leave empty for no limit. Operations that would exceed it are blocked.">
            <input name="budget" type="number" min={0} step="0.01" className={inputCls} value={f.budget} onChange={(e) => set("budget", e.target.value)} />
          </Field>
        </div>
      </Card>
      <div className="flex justify-end"><Button type="submit" disabled={act.busy}>{act.busy ? "Creating…" : "Create book & start research"}</Button></div>
    </form>
  );
}
