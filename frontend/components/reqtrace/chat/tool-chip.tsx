"use client";

import { useState } from "react";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  HashIcon,
  SearchIcon,
  ShieldCheckIcon,
  SquareCodeIcon,
  Table2Icon,
} from "lucide-react";

import type { ToolCallState, ToolCallStatus } from "@/lib/event-reducer";
import type { RagStage, ToolName } from "@/lib/events";
import { cn } from "@/lib/utils";
import { Cap } from "@/components/reqtrace/text";

/**
 * The icon per tool, shared with the checkpoint rail so a turn's dot and its
 * chip agree (canvas artboard 6).
 */
export const TOOL_ICON: Record<
  ToolName,
  React.ComponentType<{ className?: string }>
> = {
  search_requirements: SearchIcon,
  lookup_requirement: HashIcon,
  search_code: SquareCodeIcon,
  check_implementation: ShieldCheckIcon,
  generate_traceability_report: Table2Icon,
};

/**
 * The status dot's colour. Note what is NOT here: no verdict colour is a status,
 * and `unresolved` is grey rather than red. A stream that ended before its
 * result arrived is an absence, not a failure — red is reserved for a real
 * `error` (canvas `verdict-colour` note).
 */
const DOT: Record<ToolCallStatus, string> = {
  pending: "bg-source animate-pulse",
  ok: "bg-verdict-implemented",
  error: "bg-destructive",
  unresolved: "bg-verdict-missing",
};

const STATUS_LABEL: Record<ToolCallStatus, string> = {
  pending: "running",
  ok: "",
  error: "failed",
  unresolved: "no result",
};

export function ToolChip({ call }: { call: ToolCallState }) {
  const Icon = TOOL_ICON[call.tool];
  const hasStages = call.stages !== null && call.stages.length > 0;
  const hasArgs = Object.keys(call.args).length > 0;
  const expandable = hasStages || hasArgs || call.error !== null;

  // The RAG pipeline is the graded part of the project and is otherwise
  // invisible, so `search_requirements` opens by default (canvas decision 4).
  // An errored call opens too: a chip that hides its own reason is useless.
  //
  // Derived rather than seeded into state, and that matters: a live chip mounts
  // on `tool_start` when there are no stages yet, while a replayed one mounts
  // with them. Seeding the initial state would make the same events render
  // differently live and on reload. Only an explicit toggle is remembered.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open =
    toggled ??
    ((call.tool === "search_requirements" && hasStages) ||
      call.status === "error");

  const detail = call.summary ?? STATUS_LABEL[call.status];

  return (
    <div className="flex w-full flex-col gap-1.5">
      <button
        type="button"
        disabled={!expandable}
        aria-expanded={expandable ? open : undefined}
        onClick={() => setToggled(!open)}
        className={cn(
          "flex h-6 w-fit max-w-full items-center gap-1.5 rounded-[7px] border bg-card pr-[7px] pl-1.5 text-left transition-colors",
          expandable ? "hover:bg-muted" : "cursor-default",
        )}
      >
        <span
          aria-hidden
          className={cn("size-[5px] flex-none rounded-full", DOT[call.status])}
        />
        <Icon className="size-3 flex-none text-muted-foreground" />
        <span className="truncate font-mono text-[11.5px] leading-none font-medium">
          {call.tool}
        </span>
        {detail ? (
          <span
            className={cn(
              "truncate text-[11px] leading-none",
              call.status === "error"
                ? "text-destructive"
                : "text-muted-foreground",
            )}
          >
            {detail}
          </span>
        ) : null}
        {call.durationMs !== null ? (
          <span className="flex-none font-mono text-[10.5px] leading-none text-muted-foreground">
            {formatDuration(call.durationMs)}
          </span>
        ) : null}
        {expandable ? (
          open ? (
            <ChevronUpIcon className="size-3 flex-none text-muted-foreground" />
          ) : (
            <ChevronDownIcon className="size-3 flex-none text-muted-foreground" />
          )
        ) : null}
      </button>

      {expandable && open ? (
        <div className="flex flex-col gap-2 rounded-lg bg-muted px-2.5 py-2">
          {hasArgs ? (
            <div className="flex flex-col gap-0.5">
              <Cap>Arguments</Cap>
              <code className="font-mono text-[10.5px] leading-[15px] break-all text-muted-foreground">
                {formatArgs(call.args)}
              </code>
            </div>
          ) : null}

          {hasStages ? (
            <div className="flex flex-col">
              <Cap className="mb-0.5">Retrieval pipeline</Cap>
              {call.stages?.map((stage) => (
                <StageRow
                  key={stage.step}
                  stage={stage}
                  // The widest bar is the round's own largest count, so the
                  // funnel narrows visibly whatever the absolute numbers are.
                  maxCount={Math.max(
                    1,
                    ...(call.stages ?? []).map((s) => s.count ?? 0),
                  )}
                />
              ))}
            </div>
          ) : null}

          {call.error ? (
            <div className="rounded-md border border-destructive-border bg-destructive-bg px-2 py-1.5">
              <Cap className="text-destructive">Tool error</Cap>
              <p className="mt-0.5 text-[11px] leading-[15.5px]">{call.error}</p>
            </div>
          ) : null}

          {call.status === "unresolved" ? (
            <p className="text-[11px] leading-[15.5px] text-muted-foreground">
              The stream ended before this call reported a result. It may have
              completed on the backend; this thread has no record of it.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * One pipeline stage: the numbered row, plus the funnel bar when the stage
 * carries a `count` and chip rows for its queries and filters.
 *
 * A stage without the structured fields renders exactly the text row this
 * component always drew — that is the compatibility story for threads stored
 * before the fields existed, so keep the fallback branch rendering identical.
 */
function StageRow({ stage, maxCount }: { stage: RagStage; maxCount: number }) {
  const hasBar = typeof stage.count === "number";
  return (
    <div className="flex flex-col py-0.5">
      <div className="flex items-baseline gap-2">
        <span className="w-2.5 flex-none font-mono text-[10px] text-muted-foreground">
          {stage.step}
        </span>
        <span className="w-[104px] flex-none text-[11.5px] leading-4">
          {stage.label}
        </span>
        {hasBar ? (
          <>
            <span
              aria-hidden
              className="h-[5px] w-16 flex-none self-center overflow-hidden rounded-full bg-border"
            >
              <span
                className="block h-full rounded-full bg-source"
                style={{
                  width: `${stage.count === 0 ? 0 : Math.max(6, ((stage.count ?? 0) / maxCount) * 100)}%`,
                }}
              />
            </span>
            <span className="w-8 flex-none text-right font-mono text-[10.5px] leading-4">
              {stage.count}
            </span>
          </>
        ) : null}
        <span className="flex-1 font-mono text-[10.5px] leading-4 text-muted-foreground">
          {stage.detail}
        </span>
      </div>
      {stage.queries?.length || stage.filters?.length || stage.dropped_filters?.length ? (
        <div className="flex flex-wrap gap-1 py-0.5 pl-[18px]">
          {(stage.queries ?? []).concat(stage.filters ?? []).map((text) => (
            // Verbatim data, never markup — same discipline as `formatArgs`.
            <span
              key={text}
              className="rounded border bg-card px-1 py-px font-mono text-[10px] leading-[14px] text-muted-foreground"
            >
              {text}
            </span>
          ))}
          {(stage.dropped_filters ?? []).map((text) => (
            <span
              key={text}
              className="rounded border border-destructive-border bg-destructive-bg px-1 py-px font-mono text-[10px] leading-[14px] text-destructive"
            >
              {text}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * Tool arguments are rendered as data, never interpreted. `JSON.stringify` is
 * the point: whatever the model passed is shown verbatim and cannot become
 * markup or markdown.
 */
function formatArgs(args: Record<string, unknown>): string {
  return Object.entries(args)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join("  ");
}
