"use client";

import { useEffect, useState } from "react";
import { PanelRightIcon, SquareIcon } from "lucide-react";

import { VERDICTS } from "@/lib/events";
import type { LaunchResponse, ProgressData } from "@/lib/reports";
import { Button } from "@/components/ui/button";
import { Cap, Meta } from "@/components/reqtrace/text";
import { VERDICT_CLASS } from "./verdicts";

/**
 * A run in flight (story S5.5.2) — the bar, the running tallies, the spend.
 *
 * Everything here is replayed state: `ProgressData` carries the per-verdict
 * tallies and the cost so far, and `api/reports.py` re-sends the current
 * position on reattach, so closing the drawer and coming back mid-run shows
 * the same numbers it would have shown all along.
 */
export function Progress({
  launch,
  progress,
  startedAt,
  onCancel,
  onClose,
}: {
  launch: LaunchResponse;
  progress: ProgressData;
  /** When the hook entered `running` — the hook's clock, not this component's,
   *  because this component unmounts every time the drawer closes. */
  startedAt: number;
  onCancel: () => void;
  /** "Run in background" — just closes the drawer; the run does not notice. */
  onClose: () => void;
}) {
  const total = progress.total || launch.requirements;
  const pct = total ? Math.round((100 * progress.done) / total) : 0;

  // A ticking re-render, not a ticking value: elapsed time is derived from
  // `startedAt` on each render, and the interval only makes renders happen.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const elapsedSeconds = Math.max(0, Math.floor((now - startedAt) / 1000));

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="max-w-[560px]">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-[14px] font-semibold">
              <span className="font-mono">{progress.done}</span>{" "}
              <span className="font-normal text-muted-foreground">of</span>{" "}
              <span className="font-mono">{total || "?"}</span> judged
            </span>
            <Meta className="text-[11.5px]">{pct}%</Meta>
          </div>

          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-foreground transition-[width] duration-300"
              style={{ width: `${pct}%` }}
            />
          </div>

          <div className="mt-2.5 flex items-baseline gap-2">
            <Cap>Judging</Cap>
            <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]">
              {progress.current || "starting…"}
            </span>
          </div>

          <Meta className="mt-2 block text-[11px]">{launch.scope_label}</Meta>

          <div className="my-4 h-px bg-border" />

          <Cap>Verdicts so far</Cap>
          <div className="mt-1.5 flex items-stretch gap-2">
            {VERDICTS.map((verdict) => (
              <div
                key={verdict}
                className={`flex-1 rounded-lg border px-2.5 py-2 ${VERDICT_CLASS[verdict]}`}
              >
                <div className="font-mono text-[15px] leading-[18px] font-semibold">
                  {progress[verdict]}
                </div>
                <div className="mt-0.5 text-[10.5px] leading-[14px]">{verdict}</div>
              </div>
            ))}
          </div>

          <div className="my-4 h-px bg-border" />

          <div className="flex items-baseline gap-3">
            <span className="flex-1 text-[11.5px] text-muted-foreground">
              <span className="font-mono text-foreground">
                ${progress.cost_usd.toFixed(2)}
              </span>{" "}
              {launch.est_cost_usd === null
                ? "spent — no estimate was available for this scope"
                : `spent of $${launch.est_cost_usd.toFixed(2)} estimated`}
            </span>
            <Meta className="text-[11.5px]">{elapsedLabel(elapsedSeconds)}</Meta>
          </div>
        </div>
      </div>

      <div className="flex flex-none items-center gap-2 border-t px-6 py-3">
        <p className="flex-1 text-[10.5px] leading-[14px] text-muted-foreground">
          Closing this dialog does not stop the run — progress keeps streaming
          and you can keep chatting.
        </p>
        <Button variant="outline" size="sm" onClick={onClose}>
          <PanelRightIcon className="size-3.5" />
          Run in background
        </Button>
        <Button variant="destructive" size="sm" onClick={onCancel}>
          <SquareIcon className="size-3" />
          Stop
        </Button>
      </div>
    </div>
  );
}

/** `1 m 48 s`, matching the artboard's clock. */
function elapsedLabel(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} m ${seconds % 60} s`;
}
