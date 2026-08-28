"use client";

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

/** Rail padding above the first dot and below the last; dot diameter. Kept in
 *  step with the `size-[22px]`/`left-[11px]` classes on the dot itself. */
const PAD = 6;
const DOT = 22;

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
 * Dots are spread EVENLY from the first turn to the last — `index / (n - 1)` —
 * not aligned one-to-one with the turns beside them as the canvas draws them.
 * The canvas mock compresses a turn to 86 px, whereas a real exchange with
 * chips, citations and a cost line is 300–450 px tall. Mapped literally, two
 * dots would be on screen at a time and the rest unclickable, which breaks the
 * one behaviour spec §11 asks of the rail: "clicking scrolls to that exchange".
 * The controller ruled the deviation stands (fix round 1, Important 1).
 *
 * Even spacing rather than proportional-by-measurement is also deliberate. With
 * roughly equal turn heights the two are the same line, so measuring bought a
 * `ResizeObserver` plus a `requestAnimationFrame` loop plus a `revision` prop to
 * force remeasurement, and still let two short adjacent turns render as
 * overlapping 22 px circles. Even spacing has a guaranteed minimum gap by
 * construction and needs no measurement at all: the offset is a `calc()` against
 * the rail's own height, so the browser does the layout.
 */
export function CheckpointRail({
  exchanges,
  activeExchangeId,
  streamingExchangeId,
  onSelect,
}: {
  exchanges: readonly Exchange[];
  activeExchangeId: string | null;
  streamingExchangeId: string | null;
  onSelect: (exchange: Exchange) => void;
}) {
  return (
    <div className="flex w-11 flex-none flex-col border-r">
      <div className="flex h-[34px] flex-none items-center justify-center text-muted-foreground">
        <HistoryIcon className="size-3.5" />
      </div>
      <div className="relative min-h-0 flex-1 overflow-hidden">
        <span
          aria-hidden
          className="absolute top-2 bottom-2 left-[21.5px] w-px bg-border"
        />
        {exchanges.map((exchange, index) => {
          // A single-turn thread has no span to spread across, so its one dot
          // belongs at the top rather than at 0/0.
          const fraction =
            exchanges.length > 1 ? index / (exchanges.length - 1) : 0;
          return (
            <Dot
              key={exchange.id}
              exchange={exchange}
              // PAD 6 above and below, DOT 22 tall: fraction 1 lands the last
              // dot's bottom edge 6 px off the rail's bottom.
              top={`calc(${PAD}px + ${fraction} * (100% - ${DOT + 2 * PAD}px))`}
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
  top: string;
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
