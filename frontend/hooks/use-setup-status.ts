"use client";

/**
 * `GET /api/py/setup/status` — can the app work yet, and if not, what is missing?
 *
 * This is the boot gate (story S5.6.1). `/health` only reports that a process
 * is alive; this reports whether there is a corpus to answer from, whether a
 * key is configured, and how much of the index exists — which is what decides
 * between "show the app" and "show the setup screen".
 *
 * The endpoint is built never to fail: `api/deps.load_state` records a reason
 * rather than raising, so a missing `data/` directory, an unreadable database
 * and a WAL-locked one all arrive here as a sentence naming the fix instead of
 * a stack trace. The only failure this hook has to invent is the one the
 * backend cannot report — the backend not being up at all.
 *
 * It polls while an ingestion is running so the screen follows the job without
 * the caller wiring a second stream; the SSE line feed is a separate concern
 * (`useIngestLog`).
 *
 * `refresh` bumps a nonce rather than calling the fetch directly — see the
 * comment on it.
 */

import { useCallback, useEffect, useState } from "react";

/** One document as the setup screen lists it. Mirrors `DocumentStatus`. */
export interface DocumentStatus {
  key: string;
  title: string;
  module: string;
  page_count: number;
  requirements: number;
}

export type IngestJobStatus = "idle" | "running" | "succeeded" | "failed";

export interface IngestJobView {
  id: string | null;
  status: IngestJobStatus;
  lines: string[];
  error: string | null;
  restart_required: boolean;
}

/** Mirrors `SetupStatus` in `api/setup.py`, field for field. */
export interface SetupStatus {
  ready: boolean;
  api_key_present: boolean;
  index_present: boolean;
  snapshot_present: boolean;
  project_id: string | null;
  corpus_version: string | null;
  /** The manifest's pinned snapshot SHA — what every citation and verdict is
   *  actually true against. */
  git_sha: string | null;
  requirements: number;
  context_chunks: number;
  code_units: number;
  indexed_chunks: number;
  documents: DocumentStatus[];
  error: string | null;
  ingest: IngestJobView;
}

export type SetupState =
  | { phase: "checking" }
  | { phase: "unreachable" }
  | { phase: "loaded"; status: SetupStatus };

/** How often to re-ask while an ingestion is in flight. */
const POLL_MS = 2000;

export function useSetupStatus(): {
  state: SetupState;
  refresh: () => void;
} {
  const [state, setState] = useState<SetupState>({ phase: "checking" });
  const [nonce, setNonce] = useState(0);

  // Re-fetching by bumping a nonce rather than by calling a shared async
  // function is the same shape `use-backend-health` uses, and it is not a
  // style choice: React's `set-state-in-effect` rule rejects an effect that
  // reaches setState through a direct call, and accepts one that only sets
  // state inside a callback.
  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;

    fetch("/api/py/setup/status")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<SetupStatus>;
      })
      .then((status) => {
        if (!cancelled) setState({ phase: "loaded", status });
      })
      .catch(() => {
        // The backend is not up. Distinct from `status.error`, which is the
        // backend telling us what it is missing — a running server that cannot
        // answer is a different screen from no server at all.
        if (!cancelled) setState({ phase: "unreachable" });
      });

    return () => {
      cancelled = true;
    };
  }, [nonce]);

  // Follow a running ingestion without the caller having to poll.
  const running =
    state.phase === "loaded" && state.status.ingest.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [running, refresh]);

  return { state, refresh };
}
