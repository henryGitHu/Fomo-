"use client";

import { useParams } from "next/navigation";
import { useEffect } from "react";
import { useProject } from "@/components/project-context";
import { Card, Progress } from "@/components/ui";
import { money } from "@/lib/api";
import { useResource } from "@/lib/hooks";

export default function Costs() {
  const { id } = useParams<{ id: string }>();
  const { tick } = useProject();
  const { data: c, reload } = useResource<any>(`/projects/${id}/costs`, 5000);
  useEffect(() => { reload(); }, [tick, reload]);
  if (!c) return <p>Loading…</p>;
  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card title="Spent"><p className="text-2xl font-bold">{money(c.total)}</p><p className="text-xs text-slate-500">{c.generations} operations · {c.retries} with retries</p></Card>
        <Card title="Budget"><p className="text-2xl font-bold">{c.budget_limit == null ? "No limit" : money(c.budget_limit)}</p>
          {c.budget_limit != null && <><Progress value={c.total / c.budget_limit} label="Budget used" /><p className="mt-1 text-xs text-slate-500">Remaining {money(c.remaining_budget)} · reserved by queued jobs {money(c.reserved)}</p></>}
        </Card>
        <Card title="Estimated remaining"><p className="text-2xl font-bold">{money(c.estimated_remaining_cost, "Unknown")}</p><p className="text-xs text-slate-500">{c.estimated_remaining_note}</p></Card>
        <Card title="Unknown-price operations"><p className="text-2xl font-bold">{c.unknown_cost_operations}</p><p className="text-xs text-slate-500">Not included in totals — check the provider&apos;s invoice.</p></Card>
      </div>
      <Card title="By provider & model">
        <table className="w-full text-sm"><thead className="text-left text-xs text-slate-500"><tr><th>Provider</th><th>Model</th><th>Operations</th><th>Amount</th><th>Estimated / unknown</th></tr></thead>
          <tbody>{c.by_provider.map((r: any) => (
            <tr key={r.provider + r.model} className="border-t border-slate-100"><td className="py-1">{r.provider}</td><td>{r.model}</td><td>{r.count}</td><td>{money(r.amount)}</td><td>{r.estimated} est. · {r.unknown} unknown</td></tr>
          ))}</tbody></table>
      </Card>
      <Card title="By section">
        <ul className="text-sm">{c.by_section.map((s: any) => <li key={s.section_id} className="flex justify-between border-t border-slate-100 py-1"><span>{s.name}</span><span>{money(s.amount)}</span></li>)}</ul>
      </Card>
      <p className="text-xs text-slate-500">Costs come from each provider adapter&apos;s published list prices (marked as estimates) — regeneration, edits, retries and benchmarks are all included. Set the routing preference (lowest cost / balanced / highest quality within budget) in Settings.</p>
    </div>
  );
}
