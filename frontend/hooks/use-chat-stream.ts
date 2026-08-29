"use client";

/**
 * The one SSE hook (spec §7: "plain React state + one SSE hook", locked).
 *
 * It owns exactly two things: the array of events received for the message
 * currently streaming, and whether a stream is open. It derives nothing — the
 * pure reducer in `lib/event-reducer.ts` does that — and it knows nothing about
 * where events come from, because the source is injected.
 *
 * There is no state-management layer here on purpose.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatSource } from "@/lib/chat-sources";
import type { ChatEvent } from "@/lib/events";

export interface UseChatStreamOptions {
  source: ChatSource;
  /**
   * Called once when a stream finishes, for any reason, with everything that
   * arrived. This is where the caller persists the message — the events are the
   * message, so nothing else needs saving.
   */
  onSettled?: (events: ChatEvent[]) => void;
}

export interface UseChatStreamResult {
  /** Events for the in-flight message only. Empty when idle. */
  events: ChatEvent[];
  /**
   * The thread `events` belong to, or `null` before the first send.
   *
   * Returned because `events` outlive the stream: they are cleared by the
   * *next* send, not when one finishes, so between two sends they still
   * describe the thread they arrived for. A caller rendering them against a
   * different thread would be showing another conversation's answer — and,
   * because that made the transcript non-empty, would suppress the
   * empty-thread prompts and render nothing at all in their place.
   *
   * Exposed rather than cleared on thread change: clearing would mean setting
   * state from an effect, which this codebase's React rejects (see
   * `frontend/AGENTS.md`). The caller derives instead.
   */
  threadId: string | null;
  isStreaming: boolean;
  /** Rejects nothing; failures arrive as `error` events. */
  send: (threadId: string, message: string) => void;
  /** Stops the stream and settles with whatever arrived. */
  stop: () => void;
}

export function useChatStream({
  source,
  onSettled,
}: UseChatStreamOptions): UseChatStreamResult {
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [eventsThreadId, setEventsThreadId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Kept in refs so `send` does not have to be re-created every render, which
  // would restart nothing but would churn every memo below it. Written in an
  // effect rather than during render: a ref is not render output, and React's
  // lint rules are right to say so.
  const sourceRef = useRef(source);
  const settledRef = useRef(onSettled);

  useEffect(() => {
    sourceRef.current = source;
    settledRef.current = onSettled;
  });

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // The manual useCallbacks in this hook are load-bearing, not leftovers: the
  // React Compiler bails out on this file (the streaming `for await` loop and
  // the `finally` clause are constructs it cannot lower yet — check with the
  // compiler-coverage script if in doubt), so nothing memoizes these but us.
  const send = useCallback((threadId: string, message: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setEvents([]);
    setEventsThreadId(threadId);
    setIsStreaming(true);

    void (async () => {
      const received: ChatEvent[] = [];
      try {
        for await (const event of sourceRef.current({
          threadId,
          message,
          signal: controller.signal,
        })) {
          if (controller.signal.aborted) break;
          received.push(event);
          // A new array each time: the reducer is pure and callers memoise on
          // identity.
          setEvents([...received]);
        }
      } catch (cause) {
        // A source that throws instead of yielding `error` must still not hang.
        if (!controller.signal.aborted) {
          const fallback: ChatEvent = {
            type: "error",
            data: {
              message:
                "Something went wrong while streaming the answer. Whatever " +
                "arrived before the failure is kept in the thread.",
              code: "source_threw",
              retryable: true,
            },
          };
          received.push(fallback);
          setEvents([...received]);
        }
        void cause;
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
          setIsStreaming(false);
        }
        settledRef.current?.(received);
      }
    })();
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsStreaming(false);
  }, []);

  return { events, threadId: eventsThreadId, isStreaming, send, stop };
}
