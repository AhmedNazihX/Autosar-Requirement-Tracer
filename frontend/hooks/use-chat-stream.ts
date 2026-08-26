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
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Kept in a ref so `send` does not have to be re-created every render, which
  // would restart nothing but would churn every memo below it.
  const sourceRef = useRef(source);
  sourceRef.current = source;
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const send = useCallback((threadId: string, message: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setEvents([]);
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

  return { events, isStreaming, send, stop };
}
