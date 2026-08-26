"use client";

import { useEffect, useRef, useState } from "react";
import { CircleIcon, HistoryIcon } from "lucide-react";

import { clockTime } from "@/lib/format";
import type { Exchange } from "@/lib/exchanges";
import { cn } from "@/lib/utils";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Cap } from "@/components/reqtrace/text";

import { TOOL_ICON } from "./chat/tool-chip";

/**
 * The checkpoint rail — 44 px, one dot per user request, iconed by the first
 * `tool_start` of that exchange (spec §11, canvas artboard 6).
 *
 * Scope, deliberately: a click scrolls to the exchange. It does NOT restore the
 * source pane — that is F5.4, and the canvas's "restored from turn N / Return to
 * latest" bar belongs with it, so neither is drawn here. A checkpoint is a
 * bookmark: no rollback, no branching, no re-run, and no affordance hinting at
 * any of them.
 *
 * Dot positions are proportional to where the exchange sits in the WHOLE thread,
 * not to where it sits in the visible viewport. That is a deliberate departure
 * from the canvas, which draws the dots aligned one-to-one with the turns beside
 * them: the canvas mock compresses a turn to 86 px, whereas a real exchange with
 * chips, citations and a cost line is 300–450 px tall. Mapped to the viewport,
 * two dots would be visible at a time and the other four would be unclickable —
 * which breaks the one behaviour spec §11 asks of the rail, "clicking scrolls to
 * that exchange". Proportional positions keep every turn reachable and still
 * read as a timeline.
 */
export function CheckpointRail({
  exchanges,
  transcriptRef,
  revision,
  activeExchangeId,
  streamingExchangeId,
  onSelect,
}: {
  exchanges: readonly Exchange[];
  transcriptRef: React.RefObject<HTMLDivElement | null>;
  /** Bumped whenever transcript content changes, to force a re-measure. */
  revision: number;
  activeExchangeId: string | null;
  streamingExchangeId: string | null;
  onSelect: (exchange: Exchange) => void;
}) {
  const railRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);
  const fractions = useExchangeFractions(
    transcriptRef,
    revision,
    exchanges.length,
  );

  useEffect(() => {
    const rail = railRef.current;
    if (!rail) return;
    // ResizeObserver fires once on observe, so the initial height arrives
    // through the callback rather than from a synchronous write here.
    const observer = new ResizeObserver(() => setHeight(rail.clientHeight));
    observer.observe(rail);
    return () => observer.disconnect();
  }, []);

  const PAD = 6;
  const span = Math.max(height - 22 - 2 * PAD, 0);

  return (
    <div className="flex w-11 flex-none flex-col border-r">
      <div className="flex h-[34px] flex-none items-center justify-center text-muted-foreground">
        <HistoryIcon className="size-3.5" />
      </div>
      <div ref={railRef} className="relative min-h-0 flex-1 overflow-hidden">
        <span
          aria-hidden
          className="absolute top-2 bottom-2 left-[21.5px] w-px bg-border"
        />
        {height === 0
          ? null
          : exchanges.map((exchange, index) => {
              // Before the first measurement, fall back to even spacing so the
              // rail is never briefly empty on a fresh thread.
              const fraction =
                fractions[exchange.id] ??
                (exchanges.length > 1 ? index / (exchanges.length - 1) : 0);
              return (
                <Dot
                  key={exchange.id}
                  exchange={exchange}
                  top={Math.round(PAD + fraction * span)}
                  current={exchange.id === activeExchangeId}
                  running={exchange.id === streamingExchangeId}
                  onSelect={onSelect}
                />
              );
            })}
      </div>
    </div>
  );
}

function Dot({
  exchange,
  top,
  current,
  running,
  onSelect,
}: {
  exchange: Exchange;
  top: number;
  current: boolean;
  running: boolean;
  onSelect: (exchange: Exchange) => void;
}) {
  const Icon = exchange.tool ? TOOL_ICON[exchange.tool] : CircleIcon;
  const stamp = exchange.user?.created_at ?? exchange.assistant?.created_at;

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            onClick={() => onSelect(exchange)}
            aria-label={`Turn ${exchange.turn}`}
            className="absolute left-[11px] flex size-[22px] items-center justify-center"
            style={{ top }}
          >
            {running ? (
              <span
                aria-hidden
                className="absolute -inset-1 animate-spin rounded-full border-2 border-border border-t-foreground"
              />
            ) : null}
            <span
              className={cn(
                "relative flex size-[22px] items-center justify-center rounded-full border transition-colors",
                current
                  ? "border-foreground bg-foreground text-background shadow-[0_0_0_3px_var(--source-bg)]"
                  : "border-border bg-card text-muted-foreground hover:border-ring hover:bg-accent hover:text-foreground",
              )}
            >
              <Icon className="size-3" />
            </span>
          </button>
        }
      />
      <TooltipContent
        side="right"
        className="max-w-[300px] flex-col items-start gap-1.5 px-2.5 py-2 text-left"
      >
        <span className="flex w-full items-baseline gap-2">
          <span className="flex-1 text-xs font-medium">
            Turn {exchange.turn}
            {exchange.tool ? ` · ${LABELS[exchange.tool] ?? ""}` : ""}
          </span>
          {stamp ? (
            <span className="font-mono text-[10.5px] opacity-70">
              {clockTime(stamp)}
            </span>
          ) : null}
        </span>
        {exchange.user ? (
          <span className="line-clamp-2 text-[10.5px] leading-[14.5px] opacity-80">
            {exchange.user.content}
          </span>
        ) : null}
        {exchange.target ? (
          <span className="flex w-full flex-col gap-0.5">
            <Cap className="text-background/60">The source pane showed</Cap>
            <span className="font-mono text-[10.5px] leading-[14.5px]">
              {describeTarget(exchange)}
            </span>
          </span>
        ) : null}
        <span className="text-[10.5px] leading-[14.5px] opacity-70">
          Click to scroll here.
        </span>
      </TooltipContent>
    </Tooltip>
  );
}

const LABELS: Partial<Record<NonNullable<Exchange["tool"]>, string>> = {
  search_requirements: "Search",
  lookup_requirement: "Lookup",
  search_code: "Code search",
  check_implementation: "Evidence check",
  generate_traceability_report: "Report",
};

function describeTarget(exchange: Exchange): string {
  const target = exchange.target;
  if (!target) return "";
  if (target.citation.kind === "requirement") {
    return `Document · ${target.citation.req_id} p. ${target.citation.page}`;
  }
  const path = target.citation.repo_path;
  return `Code · ${path.slice(path.lastIndexOf("/") + 1)} L${target.citation.line_span[0]}–${target.citation.line_span[1]}`;
}

/**
 * Each exchange's position as a 0..1 fraction of the transcript's total scroll
 * height — a property of the thread, not of the scroll position, so scrolling
 * does not move the dots. Re-measured when the transcript's content changes.
 */
function useExchangeFractions(
  transcriptRef: React.RefObject<HTMLDivElement | null>,
  revision: number,
  count: number,
): Record<string, number> {
  const [fractions, setFractions] = useState<Record<string, number>>({});

  useEffect(() => {
    const container = transcriptRef.current;
    if (!container) return;

    let frame = 0;
    const measure = () => {
      frame = 0;
      const base = container.getBoundingClientRect().top - container.scrollTop;
      const elements = [
        ...container.querySelectorAll<HTMLElement>("[data-exchange-id]"),
      ];
      const offsets = elements.map(
        (element) => element.getBoundingClientRect().top - base,
      );
      // The rail spans first turn to last turn, so turn 1 is always at the top
      // and the newest is always at the bottom. A thread with one turn has no
      // span at all, and its single dot belongs at the top rather than nowhere.
      const first = offsets[0] ?? 0;
      const span = (offsets.at(-1) ?? 0) - first;
      const next: Record<string, number> = {};
      elements.forEach((element, index) => {
        const id = element.dataset.exchangeId;
        if (!id) return;
        const fraction = span > 0 ? (offsets[index] - first) / span : 0;
        next[id] = Math.min(Math.max(fraction, 0), 1);
      });
      setFractions(next);
    };
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(measure);
    };

    // The observer's first callback is the initial measurement, so nothing is
    // written to state synchronously from this effect.
    const observer = new ResizeObserver(schedule);
    observer.observe(container);
    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [transcriptRef, revision, count]);

  return fractions;
}
