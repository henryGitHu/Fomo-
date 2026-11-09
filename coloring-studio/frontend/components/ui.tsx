"use client";

import Link from "next/link";
import { ReactNode } from "react";
import { STATUS_COLOR, STATUS_LABEL } from "@/lib/api";

export function Button({
  children, onClick, variant = "primary", disabled, type = "button", title, className = "", ...rest
}: {
  children: ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "danger" | "ghost";
  disabled?: boolean; type?: "button" | "submit"; title?: string; className?: string; [k: string]: unknown;
}) {
  const styles = {
    primary: "bg-brand-600 text-white hover:bg-brand-700 disabled:bg-brand-600/50",
    secondary: "bg-white text-ink border border-slate-300 hover:bg-slate-50 disabled:opacity-50",
    danger: "bg-rose-600 text-white hover:bg-rose-700 disabled:opacity-50",
    ghost: "text-ink-soft hover:bg-slate-100 disabled:opacity-50",
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled} title={title}
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-1 ${styles} ${className}`}
      {...rest}>
      {children}
    </button>
  );
}

export function Card({ children, className = "", title, actions }: { children: ReactNode; className?: string; title?: ReactNode; actions?: ReactNode }) {
  return (
    <section className={`rounded-2xl border border-slate-200 bg-white p-4 shadow-sm ${className}`}>
      {(title || actions) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-base font-semibold text-ink">{title}</h2>}
          {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_COLOR[status] || "bg-slate-100 text-slate-700"}`}>
      {STATUS_LABEL[status] || status}
    </span>
  );
}

export function CheckRow({ c }: { c: { id: string; status: string; message: string; mandatory?: boolean; fix?: string | null } }) {
  const icon = c.status === "pass" ? "✓" : c.status === "warn" ? "!" : "✕";
  const color = c.status === "pass" ? "text-emerald-700" : c.status === "warn" ? "text-amber-700" : "text-rose-700";
  return (
    <li className="flex gap-2 text-sm">
      <span className={`w-4 shrink-0 font-bold ${color}`} aria-label={c.status}>{icon}</span>
      <span>
        <span className="font-medium">{c.id.replace(/_/g, " ")}</span>
        {c.mandatory ? <span className="ml-1 text-xs text-slate-500">(required)</span> : null}: {c.message}
        {c.fix && c.status !== "pass" ? <span className="block text-xs text-slate-500">Fix: {c.fix}</span> : null}
      </span>
    </li>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-ink">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

/** Base input style without width, for inline controls (combine with w-auto / w-28 …). */
export const inputBase =
  "rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100";
export const inputCls = `w-full ${inputBase}`;

export function ErrorNote({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-800">{error}</p>;
}

export function Progress({ value, label }: { value: number; label?: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
        <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function NavLink({ href, active, children }: { href: string; active: boolean; children: ReactNode }) {
  return (
    <Link href={href} aria-current={active ? "page" : undefined}
      className={`whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium ${active ? "bg-brand-600 text-white" : "text-ink-soft hover:bg-slate-100"}`}>
      {children}
    </Link>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">{children}</p>;
}
