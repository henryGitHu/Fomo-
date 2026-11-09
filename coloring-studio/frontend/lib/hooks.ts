"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** Fetch a resource, optionally polling. Returns data, error, reload. */
export function useResource<T>(path: string | null, pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const alive = useRef(true);

  const reload = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e: any) {
      if (alive.current) setError(e.message || String(e));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    reload();
    let t: ReturnType<typeof setInterval> | undefined;
    if (pollMs) t = setInterval(reload, pollMs);
    return () => {
      alive.current = false;
      if (t) clearInterval(t);
    };
  }, [reload, pollMs]);

  return { data, error, loading, reload, setData };
}

/** Run an async action with busy/error state. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e: any) {
      setError(e.message || String(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, setError };
}
