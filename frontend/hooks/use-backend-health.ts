"use client";

/**
 * The one real network call this build makes: `GET /api/py/health`.
 *
 * It goes through the Next.js rewrites proxy, so there is no CORS config
 * anywhere. It lives in a hook rather than in the sidebar because a component
 * should not own a fetch — the same rule that puts thread access behind
 * `lib/threads.ts`.
 *
 * `/health` reports liveness and a version. It does NOT report index counts, so
 * the sidebar footer says what it knows and does not invent requirement or
 * code-unit totals.
 */

import { useCallback, useEffect, useState } from "react";

export type BackendHealth =
  | { status: "checking" }
  | { status: "ok"; version: string }
  | { status: "unreachable" };

export function useBackendHealth(): {
  health: BackendHealth;
  recheck: () => void;
} {
  const [health, setHealth] = useState<BackendHealth>({ status: "checking" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;

    fetch("/api/py/health")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<{ status: string; version: string }>;
      })
      .then((data) => {
        if (!cancelled) setHealth({ status: "ok", version: data.version });
      })
      .catch(() => {
        if (!cancelled) setHealth({ status: "unreachable" });
      });

    return () => {
      cancelled = true;
    };
  }, [nonce]);

  // "checking" is set here, in the event handler, rather than in the effect —
  // the effect only ever writes state from the fetch's own callbacks.
  const recheck = useCallback(() => {
    setHealth({ status: "checking" });
    setNonce((value) => value + 1);
  }, []);

  return { health, recheck };
}
