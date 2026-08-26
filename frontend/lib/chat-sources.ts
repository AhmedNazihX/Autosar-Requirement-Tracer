/**
 * The two chat sources. One signature, two bodies.
 *
 *   ChatSource = (request) => AsyncIterable<ChatEvent>
 *
 * `cannedSource` replays the committed fixture. `sseSource` streams
 * `POST /api/py/chat` for real. `useChatStream` consumes whichever it is given
 * and no component knows the difference — swapping the source is a one-line
 * change in `pickChatSource`, not a refactor.
 *
 * Selection is by build-time env so the UI stays exactly as the canvas drew it
 * (no dev-only toggle on screen):
 *
 *   NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=canned   (default)
 *   NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=live     POST /api/py/chat
 *
 * `POST /chat` exists as of WP3 (story S3.3.1), so the live source works
 * against a running backend. `canned` stays the default so the committed demo
 * conversation renders with no backend and no API key at all; set
 * NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=live to drive the real one.
 */

import { isChatEvent, type ChatEvent } from "./events";
import { CANNED_EXCHANGES } from "./fixtures/canned-conversation";

export interface ChatRequest {
  threadId: string;
  message: string;
  signal: AbortSignal;
}

export type ChatSource = (request: ChatRequest) => AsyncIterable<ChatEvent>;

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Pacing that makes each event type legible while driving the app by hand. */
function delayFor(event: ChatEvent): number {
  switch (event.type) {
    case "token":
      return 26;
    case "tool_start":
      return 420;
    case "tool_result":
      return 620;
    case "citation":
      return 90;
    case "usage":
      return 200;
    default:
      return 140;
  }
}

/**
 * Replays the committed exchanges in order, cycling so every send produces a
 * full stream. The fifth exchange ends without `done` on purpose: the caller
 * must handle a stream that just stops.
 */
export function cannedSource(): ChatSource {
  let next = 0;
  return async function* canned({ signal }) {
    const exchange = CANNED_EXCHANGES[next % CANNED_EXCHANGES.length];
    next += 1;
    for (const event of exchange.events) {
      await sleep(delayFor(event), signal);
      yield event;
    }
  };
}

/**
 * Streams `POST /api/py/chat` (spec §6). Reached only through the Next.js
 * rewrites proxy, so there is no CORS configuration anywhere.
 *
 * A transport failure — no backend, a non-2xx status, a socket that dies
 * mid-stream — is turned into an `error` event rather than a thrown promise, so
 * the UI path for "OpenRouter returned 502" and the UI path for "the backend is
 * not running" are the same path, and neither can hang.
 */
export function sseSource(): ChatSource {
  return async function* sse({ threadId, message, signal }) {
    let response: Response;
    try {
      response = await fetch("/api/py/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ thread_id: threadId, message }),
        signal,
      });
    } catch (cause) {
      if (signal.aborted) return;
      yield {
        type: "error",
        data: {
          message:
            "Could not reach the ReqTrace backend. Start it with `make dev` " +
            "and send the message again — nothing was lost.",
          code: "backend_unreachable",
          retryable: true,
        },
      };
      void cause;
      return;
    }

    if (!response.ok || !response.body) {
      yield {
        type: "error",
        data: {
          message: describeHttpFailure(response.status),
          code: `http_${response.status}`,
          retryable: response.status >= 500,
        },
      };
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line; `data:` lines carry the
        // {type, data} envelope.
        let split = buffer.indexOf("\n\n");
        while (split !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const event = parseFrame(frame);
          if (event) yield event;
          split = buffer.indexOf("\n\n");
        }
      }
    } catch (cause) {
      if (signal.aborted) return;
      yield {
        type: "error",
        data: {
          message:
            "The answer stopped mid-stream: the connection to the backend was " +
            "lost. The tokens received so far are saved in the thread.",
          code: "stream_interrupted",
          retryable: true,
        },
      };
      void cause;
    } finally {
      reader.releaseLock();
    }
  };
}

/**
 * One user-readable sentence per failure mode, and no status code without an
 * explanation beside it.
 *
 * The 5xx wording covers both cases on purpose: the Next.js `rewrites()` proxy
 * cannot forward to a backend that is not listening, so it answers 500 itself.
 * From the browser, "the backend is not running" and "the backend threw" are the
 * same response, and claiming to know which one it was would be a guess.
 */
function describeHttpFailure(status: number): string {
  if (status === 404) {
    return (
      "The backend is running but has no /chat endpoint, so it is older than " +
      "this build of the UI. Restart it from the current backend/ directory. " +
      "Nothing was sent to a model."
    );
  }
  if (status >= 500) {
    return (
      `The backend did not answer (HTTP ${status}). Either it is not running — ` +
      "start it with `make dev` — or it failed while handling the request. " +
      "Your question is saved in the thread."
    );
  }
  return `The backend refused the request (HTTP ${status}).`;
}

function parseFrame(frame: string): ChatEvent | null {
  const payload = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!payload) return null;
  try {
    const parsed: unknown = JSON.parse(payload);
    // Anything off-contract is dropped rather than rendered. The event model in
    // lib/events.ts is the contract; the wire does not get to widen it.
    return isChatEvent(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export type ChatSourceKind = "canned" | "live";

export function chatSourceKind(): ChatSourceKind {
  return process.env.NEXT_PUBLIC_REQTRACE_CHAT_SOURCE === "live"
    ? "live"
    : "canned";
}

export function pickChatSource(): ChatSource {
  return chatSourceKind() === "live" ? sseSource() : cannedSource();
}
