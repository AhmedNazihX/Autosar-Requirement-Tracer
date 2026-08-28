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

/** Rail padding above the first dot and below the last; dot diameter. Kept in
 *  step with the `size-[22px]`/`left-[11px]` classes on the dot itself. */
const PAD = 6;
const DOT = 22;
/** Vertical stagger between dots piled at a clamp edge, and how many of a pile
 *  get their own offset before the rest render coincident. */
const PILE_STEP = 6;
const PILE_MAX = 4;

interface DotPosition {
  id: string;
  top: number;
  /** Set when the message is scrolled out of the transcript viewport and the
   *  dot is pinned at a rail edge instead of beside its message. */
  clamped: boolean;
}

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
 * Dots sit LEVEL WITH THEIR MESSAGES, scroll-synced, as the canvas draws them.
 * This supersedes the fix-round-1 ruling that spread them evenly: the user
 * directed the alignment on 2026-08-28, and the reachability problem that
 * motivated even spacing (a real exchange is 300–450 px tall, so only two or
 * three messages fit the viewport) is answered by clamping instead — a dot
 * whose message has scrolled away pins at the nearest rail edge, faded but
 * still clickable, so "clicking scrolls to that exchange" survives for every
 * turn.
 *
 * Positions are `getBoundingClientRect()` deltas between the message element
 * and the rail track, both viewport-relative, so the chat header, the rail
 * header and any ancestor offsets cancel without walking offsetParent chains.
 * One rAF-throttled measure runs on transcript scroll, on any observed resize
 * (a streaming answer grows its exchange), and when the turn list changes.
 */
export function CheckpointRail({
  exchanges,
  activeExchangeId,
  streamingExchangeId,
  transcriptRef,
  onSelect,
}: {
  exchanges: readonly Exchange[];
  activeExchangeId: string | null;
  streamingExchangeId: string | null;
  /** The chat transcript's scroll container — the same ref the shell hands
   *  `ChatPane`, whose children carry `data-exchange-id` anchors. */
  transcriptRef: React.RefObject<HTMLDivElement | null>;
  onSelect: (exchange: Exchange) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const positions = useDotPositions(transcriptRef, trackRef, exchanges);
  const byId = new Map(positions?.map((p) => [p.id, p]) ?? []);

  return (
    <div className="flex w-11 flex-none flex-col border-r">
      <div className="flex h-[34px] flex-none items-center justify-center text-muted-foreground">
        <HistoryIcon className="size-3.5" />
      </div>
      <div ref={trackRef} className="relative min-h-0 flex-1 overflow-hidden">
        <span
          aria-hidden
          className="absolute top-2 bottom-2 left-[21.5px] w-px bg-border"
        />
        {exchanges.map((exchange) => {
          const position = byId.get(exchange.id);
          return (
            <Dot
              key={exchange.id}
              exchange={exchange}
              // Effects run after paint, so the first frame has no measurement
              // yet; an invisible dot beats one that jumps from the top.
              top={position ? position.top : null}
              clamped={position?.clamped ?? false}
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

/**
 * Dot tops for every exchange, in rail-track coordinates, re-measured on
 * scroll, resize and turn-list changes. `null` until the first measure.
 */
function useDotPositions(
  transcriptRef: React.RefObject<HTMLDivElement | null>,
  trackRef: React.RefObject<HTMLDivElement | null>,
  exchanges: readonly Exchange[],
): DotPosition[] | null {
  const [positions, setPositions] = useState<DotPosition[] | null>(null);
  const frameRef = useRef(0);

  useEffect(() => {
    const transcript = transcriptRef.current;
    const track = trackRef.current;
    if (!transcript || !track) return;

    const measure = () => {
      frameRef.current = 0;
      const trackRect = track.getBoundingClientRect();
      const viewport = transcript.getBoundingClientRect();
      const min = PAD;
      const max = trackRect.height - DOT - PAD;

      // Whether a dot clamps is judged against the TRANSCRIPT's viewport —
      // "is the message on screen" — not against the rail track, whose top
      // sits a header-height lower; judged by the track, the first visible
      // message would read as scrolled-away. Where the dot lands is still
      // track-relative, clipped into the track's own bounds.
      const raw: { id: string; top: number; away: -1 | 0 | 1 }[] = [];
      for (const element of transcript.querySelectorAll<HTMLElement>(
        "[data-exchange-id]",
      )) {
        const id = element.dataset.exchangeId;
        if (!id) continue;
        const rect = element.getBoundingClientRect();
        const away = rect.bottom < viewport.top ? -1 : rect.top > viewport.bottom ? 1 : 0;
        raw.push({ id, top: rect.top - trackRect.top, away });
      }

      // Scrolled-away messages pile at the rail edges. The stagger keeps turn
      // order reading top-to-bottom inside a pile — the turn nearest the
      // viewport steps furthest inward, the rest walk back to the edge and
      // past PILE_MAX render coincident, so a deep pile never marches over
      // the in-view dots.
      const above = raw.filter((p) => p.away === -1);
      const below = raw.filter((p) => p.away === 1);
      const step = (fromViewport: number) =>
        Math.max(0, PILE_MAX - 1 - Math.min(fromViewport, PILE_MAX - 1)) *
        PILE_STEP;
      const next: DotPosition[] = raw.map((p) => {
        if (p.away === -1) {
          const fromViewport = above.length - 1 - above.indexOf(p);
          return { id: p.id, top: min + step(fromViewport), clamped: true };
        }
        if (p.away === 1) {
          const fromViewport = below.indexOf(p);
          return { id: p.id, top: max - step(fromViewport), clamped: true };
        }
        return {
          id: p.id,
          top: Math.min(Math.max(p.top, min), max),
          clamped: false,
        };
      });
      setPositions(next);
    };

    const schedule = () => {
      if (frameRef.current) return;
      frameRef.current = requestAnimationFrame(measure);
    };

    schedule();
    transcript.addEventListener("scroll", schedule, { passive: true });
    const observer = new ResizeObserver(schedule);
    observer.observe(transcript);
    observer.observe(track);
    for (const element of transcript.querySelectorAll<HTMLElement>(
      "[data-exchange-id]",
    )) {
      observer.observe(element);
    }

    return () => {
      transcript.removeEventListener("scroll", schedule);
      observer.disconnect();
      // Cancel AND reset: the effect re-runs on every turn-list change, and a
      // frame is almost always pending at teardown (ResizeObserver fires an
      // initial callback on observe). A stale id left in the ref makes every
      // later schedule() no-op — dots then stay hidden forever, silently.
      // That exact bug shipped once; this comment is its tombstone.
      if (frameRef.current) cancelAnimationFrame(frameRef.current);
      frameRef.current = 0;
    };
  }, [transcriptRef, trackRef, exchanges]);

  return positions;
}

function Dot({
  exchange,
  top,
  clamped,
  current,
  running,
  onSelect,
}: {
  exchange: Exchange;
  top: number | null;
  clamped: boolean;
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
            className={cn(
              "absolute left-[11px] flex size-[22px] items-center justify-center",
              clamped && "opacity-60",
            )}
            style={
              top === null
                ? { visibility: "hidden", top: 0 }
                : { top: `${top}px` }
            }
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
