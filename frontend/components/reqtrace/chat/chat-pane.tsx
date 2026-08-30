"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUpIcon, SquareIcon } from "lucide-react";

import type { MessageRenderState } from "@/lib/event-reducer";
import { reduceEvents } from "@/lib/event-reducer";
import type {
  ChatEvent,
  CodeCitation,
  RequirementCitation,
} from "@/lib/events";
import type { Exchange } from "@/lib/exchanges";
import type { SetupStatus } from "@/hooks/use-setup-status";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Kbd } from "@/components/reqtrace/text";

import { AssistantMessage } from "./assistant-message";
import { EmptyThread } from "./empty-thread";

/**
 * The chat pane.
 *
 * It renders exchanges, and an exchange's assistant half is always the output of
 * the pure reducer over a stored event array — with exactly one exception, the
 * message currently streaming, whose events live in the SSE hook until it
 * settles. Both go through `reduceEvents`; the only difference is the `live`
 * flag, which is what stops a killed stream from leaving a chip spinning.
 */
export function ChatPane({
  exchanges,
  liveEvents,
  isStreaming,
  transcriptRef,
  flashExchangeId,
  setup = null,
  onSend,
  onStop,
  onOpenCitation,
  onRetry,
}: {
  exchanges: readonly Exchange[];
  liveEvents: readonly ChatEvent[];
  isStreaming: boolean;
  transcriptRef: React.RefObject<HTMLDivElement | null>;
  flashExchangeId: string | null;
  /** Live corpus counts for the empty state; null in canned mode. */
  setup?: SetupStatus | null;
  onSend: (text: string) => void;
  onStop: () => void;
  onOpenCitation: (citation: RequirementCitation | CodeCitation) => void;
  onRetry: () => void;
}) {
  const [draft, setDraft] = useState("");
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const atBottomRef = useRef(true);

  const liveState: MessageRenderState | null =
    isStreaming || liveEvents.length > 0
      ? reduceEvents(liveEvents, { live: isStreaming })
      : null;

  // Follow the stream, but only while the reader is already at the bottom.
  // Yanking the transcript down while somebody is reading an earlier answer is
  // the single most annoying thing a chat UI can do.
  useLayoutEffect(() => {
    const container = transcriptRef.current;
    if (!container || !atBottomRef.current) return;
    container.scrollTop = container.scrollHeight;
  }, [transcriptRef, exchanges, liveEvents.length]);

  useEffect(() => {
    const container = transcriptRef.current;
    if (!container) return;
    const onScroll = () => {
      const distance =
        container.scrollHeight - container.scrollTop - container.clientHeight;
      atBottomRef.current = distance < 48;
    };
    container.addEventListener("scroll", onScroll, { passive: true });
    return () => container.removeEventListener("scroll", onScroll);
  }, [transcriptRef]);

  function submit() {
    const text = draft.trim();
    if (!text || isStreaming) return;
    setDraft("");
    onSend(text);
  }

  const isEmpty = exchanges.length === 0 && !liveState;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        ref={transcriptRef}
        className="min-h-0 flex-1 overflow-y-auto px-4"
        aria-live="polite"
      >
        {isEmpty ? (
          <EmptyThread
            setup={setup}
            onPick={(prompt) => {
              setDraft(prompt);
              composerRef.current?.focus();
            }}
          />
        ) : (
          <div className="flex flex-col gap-4 py-4">
            {exchanges.map((exchange, index) => {
              const isLast = index === exchanges.length - 1;
              const state = exchange.assistant
                ? reduceEvents(exchange.assistant.events)
                : isLast
                  ? liveState
                  : null;
              return (
                <div
                  key={exchange.id}
                  data-exchange-id={exchange.id}
                  className={cn(
                    "flex scroll-mt-4 flex-col gap-3.5 rounded-xl transition-shadow",
                    flashExchangeId === exchange.id &&
                      "ring-2 ring-ring ring-offset-2 ring-offset-background",
                  )}
                >
                  {exchange.user ? (
                    <div className="flex justify-end">
                      <div className="max-w-[400px] rounded-xl bg-muted px-3 py-2 text-sm leading-5 whitespace-pre-wrap">
                        {exchange.user.content}
                      </div>
                    </div>
                  ) : null}

                  {state ? (
                    <AssistantMessage
                      state={state}
                      onOpenCitation={onOpenCitation}
                      onRetry={isLast ? onRetry : undefined}
                    />
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="flex-none px-4 pt-2 pb-3.5">
        <div className="rounded-lg border bg-card px-3 pt-2 pb-1.5">
          <Textarea
            ref={composerRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
            rows={2}
            placeholder="Ask about a requirement, or paste an ID like SWS_CANIF_00064"
            className="min-h-[38px] resize-none border-0 bg-transparent p-0 text-[13.5px] leading-[19px] shadow-none focus-visible:ring-0 dark:bg-transparent"
          />
          <div className="mt-0.5 flex items-center gap-2">
            <span className="flex-1 text-[10.5px] text-muted-foreground">
              <Kbd>Enter</Kbd> to send · <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> for a
              newline
            </span>
            {isStreaming ? (
              <Button
                size="icon-sm"
                variant="outline"
                onClick={onStop}
                aria-label="Stop generating"
              >
                <SquareIcon />
              </Button>
            ) : (
              <Button
                size="icon-sm"
                onClick={submit}
                disabled={draft.trim().length === 0}
                aria-label="Send message"
              >
                <ArrowUpIcon />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
