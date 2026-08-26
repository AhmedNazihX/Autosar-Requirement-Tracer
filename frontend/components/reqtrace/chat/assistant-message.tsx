"use client";

import type { MessageRenderState } from "@/lib/event-reducer";
import type { CodeCitation, RequirementCitation } from "@/lib/events";
import { useThrottled } from "@/hooks/use-throttled";
import { Skeleton } from "@/components/ui/skeleton";
import { MarkdownBody } from "@/components/reqtrace/markdown-body";

import { CitationChips } from "./citation-chips";
import { ErrorNotice } from "./error-notice";
import { ToolChip } from "./tool-chip";
import { UsageLine } from "./usage-line";

/**
 * One assistant message, rendered entirely from its `MessageRenderState`.
 *
 * Every element here — the text, each chip, the cost line, the error banner — is
 * a field the pure reducer computed from that message's event array. Nothing is
 * read from anywhere else, which is exactly why a reload replays a thread
 * identically: the same events go through the same reducer into the same
 * component.
 */
export function AssistantMessage({
  state,
  onOpenCitation,
  onRetry,
}: {
  state: MessageRenderState;
  onOpenCitation: (citation: RequirementCitation | CodeCitation) => void;
  onRetry?: () => void;
}) {
  // The markdown renderer sees a value that changes at most ten times a second,
  // not once per `token` delta.
  const text = useThrottled(state.text, 100);
  const streaming = state.status === "streaming";
  const awaitingText = streaming && text.length === 0;

  return (
    <div className="flex flex-col gap-2.5">
      {state.toolCalls.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          {state.toolCalls.map((call) => (
            <ToolChip key={call.id} call={call} />
          ))}
        </div>
      ) : null}

      {awaitingText ? (
        <div className="flex flex-col gap-1.5" aria-label="Answer loading">
          <Skeleton className="h-3.5 w-[92%]" />
          <Skeleton className="h-3.5 w-[78%]" />
          <Skeleton className="h-3.5 w-[55%]" />
        </div>
      ) : text.length > 0 ? (
        <div className="relative">
          <MarkdownBody text={text} />
          {streaming ? (
            <span
              aria-hidden
              className="ml-0.5 inline-block h-[14px] w-[2px] translate-y-[2px] animate-pulse bg-foreground align-baseline"
            />
          ) : null}
        </div>
      ) : null}

      {state.error ? (
        <ErrorNotice error={state.error} onRetry={onRetry} />
      ) : null}

      {state.status === "interrupted" && !state.error ? (
        <p className="text-[11px] leading-[15.5px] text-muted-foreground">
          The stream ended without a completion event. Everything above is what
          the thread stored.
        </p>
      ) : null}

      <CitationChips
        sources={state.sources}
        upstream={state.upstream}
        onOpen={onOpenCitation}
      />

      {state.usage ? <UsageLine usage={state.usage} /> : null}
    </div>
  );
}
