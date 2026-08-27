"use client";

/**
 * Running one traceability report, from launch to matrix (feature F5.5).
 *
 * The lifecycle is three states and the drawer renders one per state: nothing
 * started, a run in flight, a finished matrix. This hook owns all three so the
 * drawer stays a view.
 *
 * **Progress comes from the SSE stream, the result from a fetch.** The stream
 * carries `{done, total, current}` and a terminal frame — deliberately not the
 * matrix, which for 398 rows is a quarter of a megabyte nobody needs until the
 * run ends. So the terminal frame triggers one `GET /reports/{id}`.
 *
 * **The stream is re-attachable, and that is the point.** `api/reports.py`
 * replays the current position before following, so closing and reopening the
 * drawer mid-run — or launching, navigating away and coming back — picks the
 * run up where it is rather than showing nothing.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelRun,
  eventsUrl,
  getRun,
  isReportFrame,
  launchReport,
  type LaunchResponse,
  type ProgressData,
  type ReportResult,
  type ReportScope,
  type RunStatus,
} from "@/lib/reports";
import { parseSseFrame, sseBlocks } from "@/lib/sse";

export type ReportPhase =
  | { phase: "idle" }
  | { phase: "launching" }
  | { phase: "running"; jobId: string; launch: LaunchResponse; progress: ProgressData }
  | {
      phase: "finished";
      jobId: string;
      status: RunStatus;
      result: ReportResult | null;
      error: string | null;
    }
  | { phase: "failed"; message: string };

export function useReport(): {
  state: ReportPhase;
  launch: (scope: ReportScope) => Promise<void>;
  cancel: () => void;
  reset: () => void;
  attach: (jobId: string) => void;
} {
  const [state, setState] = useState<ReportPhase>({ phase: "idle" });
  // The job the stream should be following. Separate from `state` so the
  // effect below has one dependency rather than pattern-matching a union.
  const [following, setFollowing] = useState<string | null>(null);
  const launchRef = useRef<LaunchResponse | null>(null);

  const launch = useCallback(async (scope: ReportScope) => {
    setState({ phase: "launching" });
    try {
      const response = await launchReport(scope);
      launchRef.current = response;
      setState({
        phase: "running",
        jobId: response.job_id,
        launch: response,
        progress: { done: 0, total: response.requirements, current: "" },
      });
      setFollowing(response.job_id);
    } catch (cause) {
      setState({
        phase: "failed",
        message:
          cause instanceof Error ? cause.message : "The report could not be started.",
      });
    }
  }, []);

  const attach = useCallback((jobId: string) => setFollowing(jobId), []);

  const cancel = useCallback(() => {
    if (following) void cancelRun(following);
  }, [following]);

  const reset = useCallback(() => {
    setFollowing(null);
    launchRef.current = null;
    setState({ phase: "idle" });
  }, []);

  useEffect(() => {
    if (!following) return;
    const controller = new AbortController();

    // Named so the terminal frame and the stream ending share one path: a run
    // that finishes between frames must still fetch its matrix.
    const finish = async () => {
      try {
        const run = await getRun(following);
        if (controller.signal.aborted) return;
        setState({
          phase: "finished",
          jobId: following,
          status: run?.status ?? "failed",
          result: run?.result ?? null,
          error: run?.error ?? null,
        });
      } catch (cause) {
        if (controller.signal.aborted) return;
        setState({
          phase: "failed",
          message:
            cause instanceof Error ? cause.message : "The report result could not be read.",
        });
      }
    };

    void (async () => {
      try {
        const response = await fetch(eventsUrl(following), {
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        for await (const block of sseBlocks(response.body)) {
          const frame = parseSseFrame(block, isReportFrame);
          if (!frame) continue;
          if (frame.type === "progress") {
            const progress = frame.data;
            setState((current) =>
              current.phase === "running"
                ? { ...current, progress }
                : {
                    // Re-attached to a run this tab did not launch.
                    phase: "running",
                    jobId: following,
                    launch:
                      launchRef.current ?? placeholderLaunch(following, progress.total),
                    progress,
                  },
            );
          } else if (frame.type === "error") {
            setState({ phase: "failed", message: frame.data.message });
            return;
          } else {
            await finish();
            return;
          }
        }
        // The stream closed without a terminal frame — the run may still have
        // finished, so ask rather than leaving a progress bar up forever.
        if (!controller.signal.aborted) await finish();
      } catch (cause) {
        if (controller.signal.aborted) return;
        setState({
          phase: "failed",
          message:
            "Lost the connection to the report. It may still be running — reopen " +
            "the drawer to reattach.",
        });
        void cause;
      }
    })();

    return () => controller.abort();
  }, [following]);

  return { state, launch, cancel, reset, attach };
}

function placeholderLaunch(jobId: string, total: number): LaunchResponse {
  return {
    job_id: jobId,
    status: "running",
    scope_label: "a report already running",
    requirements: total,
    to_judge: total,
    cached: 0,
    est_cost_usd: null,
    est_basis: "",
    ceiling_usd: 0,
    unknown_ids: [],
  };
}

