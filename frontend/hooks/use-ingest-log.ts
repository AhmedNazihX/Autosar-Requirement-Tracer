"use client";

/**
 * `GET /api/py/setup/ingest/events` — the ingestion job's progress lines.
 *
 * The backend built this stream for this screen (story S3.6.2), and it earns
 * its place over polling `setup/status`'s `ingest.lines`: a cold ingestion runs
 * ~44 seconds and a 2-second poll delivers it in batches, which reads as a
 * stalled screen punctuated by jumps. The stream delivers each line as the
 * pipeline emits it.
 *
 * The two are not redundant. This owns the *log*; `useSetupStatus` owns
 * *readiness*. Deriving readiness from log text would mean parsing prose.
 *
 * The stream replays everything already emitted before following, so opening
 * the screen mid-run — or reopening it after a reload — shows the whole run
 * rather than whatever happens next.
 */

import { useCallback, useEffect, useState } from "react";

import type { IngestStep } from "@/hooks/use-setup-status";
import { parseSseFrame, sseBlocks } from "@/lib/sse";

/** The `{type, data}` frames `api/setup.py::ingest_events` emits. */
type IngestFrame =
  | { type: "line"; data: { text: string } }
  | { type: "steps"; data: { steps: IngestStep[] } }
  | { type: "idle"; data: { text: string } }
  | { type: "succeeded"; data: { text: string } }
  | { type: "failed"; data: { text: string } }
  | { type: "cancelled"; data: { text: string } };

export type IngestPhase =
  | "idle"
  | "streaming"
  | "succeeded"
  | "failed"
  | "cancelled";

function isFrame(value: unknown): value is IngestFrame {
  if (typeof value !== "object" || value === null) return false;
  const frame = value as { type?: unknown; data?: unknown };
  if (typeof frame.data !== "object" || frame.data === null) return false;
  if (frame.type === "steps")
    return Array.isArray((frame.data as { steps?: unknown }).steps);
  return (
    (frame.type === "line" ||
      frame.type === "idle" ||
      frame.type === "succeeded" ||
      frame.type === "failed" ||
      frame.type === "cancelled") &&
    typeof (frame.data as { text?: unknown }).text === "string"
  );
}

export function useIngestLog(active: boolean): {
  lines: string[];
  /** The step tracker's state — `null` until the stream's first snapshot. */
  steps: IngestStep[] | null;
  phase: IngestPhase;
  error: string | null;
  reset: () => void;
} {
  const [lines, setLines] = useState<string[]>([]);
  const [steps, setSteps] = useState<IngestStep[] | null>(null);
  const [outcome, setOutcome] = useState<
    "succeeded" | "failed" | "cancelled" | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  // `phase` is derived, not stored. Storing it would mean writing "streaming"
  // synchronously inside the effect that opens the stream, which React's
  // `set-state-in-effect` rule rejects — and the derived form is simply true:
  // while the stream is open and nothing terminal has arrived, it is streaming.
  const phase: IngestPhase = outcome ?? (active ? "streaming" : "idle");

  // Load-bearing useCallback, not a leftover: the React Compiler bails out on
  // this file (the SSE `for await` loop is a construct it cannot lower yet),
  // so nothing memoizes this but us.
  const reset = useCallback(() => {
    setLines([]);
    setSteps(null);
    setOutcome(null);
    setError(null);
  }, []);

  useEffect(() => {
    if (!active) return;

    const controller = new AbortController();

    void (async () => {
      try {
        const response = await fetch("/api/py/setup/ingest/events", {
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}`);
        }
        for await (const block of sseBlocks(response.body)) {
          const frame = parseSseFrame(block, isFrame);
          if (!frame) continue;
          if (frame.type === "line") {
            setLines((previous) => [...previous, frame.data.text]);
          } else if (frame.type === "steps") {
            setSteps(frame.data.steps);
          } else if (frame.type === "succeeded") {
            setOutcome("succeeded");
          } else if (frame.type === "cancelled") {
            setOutcome("cancelled");
          } else if (frame.type === "failed") {
            setOutcome("failed");
            setError(frame.data.text || "Ingestion failed.");
          }
          // An `idle` frame means no job has ever been started; the derived
          // phase already says that, so there is nothing to record.
        }
      } catch (cause) {
        if (controller.signal.aborted) return;
        setOutcome("failed");
        setError(
          "Lost the connection to the ingestion log. The run may still be " +
            "going — reload to reattach.",
        );
        void cause;
      }
    })();

    return () => controller.abort();
  }, [active]);

  return { lines, steps, phase, error, reset };
}

