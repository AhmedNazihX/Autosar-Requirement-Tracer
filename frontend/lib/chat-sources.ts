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
 *   NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=live     (default) POST /api/py/chat
 *   NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=canned   replay the committed fixture
 *
 * **`live` became the default in WP5.** Until then the backend did not exist
 * and the fixture was the only thing to show. It does now, and a build whose
 * default is a fixture makes the real application the thing you have to opt
 * into — while `GET /setup/status` plus the setup screen (story S5.6.1)
 * already handle every reason the backend might not be usable, which is the
 * job `canned` was standing in for.
 *
 * `canned` is kept, and kept working: it is the only way to see all five event
 * types with no backend, no index and no API key, which is worth having for a
 * demo on a machine that has none of them.
 *
 * `lib/threads.ts` reads :func:`chatSourceKind` too, so threads and chat are
 * never in different worlds — see the note there.
 */

import { isChatEvent, type ChatEvent } from "./events";
import { CANNED_EXCHANGES } from "./fixtures/canned-conversation";
import { parseSseFrame, sseBlocks } from "./sse";

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
          // 429 is retryable by construction: the bucket refills, so the
          // same request succeeds a moment later. Without this the one
          // failure the user is *meant* to just retry offers no Retry button.
          retryable: response.status >= 500 || response.status === 429,
        },
      };
      return;
    }

    try {
      for await (const block of sseBlocks(response.body)) {
        const event = parseSseFrame(block, isChatEvent);
        if (event) yield event;
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
  if (status === 429) {
    // The backend paces `POST /chat` (story S6.3.1) because each turn calls a
    // paid model several times. A person never reaches this; a render loop or
    // a held Retry does, which is exactly what the limit is for.
    return (
      "Too many questions at once — the backend is pacing requests so a retry " +
      "loop cannot run up a model bill. Wait a moment and press Retry; your " +
      "question is still here."
    );
  }
  if (status >= 500) {
    // Deliberately does NOT promise the question was saved. When the backend
    // is down, `POST /chat` is what would have stored it — so it was not, and
    // the transcript is showing it from memory (`app-shell.tsx`, `unsaved`).
    // Claiming otherwise was measured to be false: killing the API mid-session
    // produced this exact message above a question that existed nowhere but
    // that tab.
    return (
      `The backend did not answer (HTTP ${status}). Either it is not running — ` +
      "start it with `make dev` — or it failed while handling the request. " +
      "Your question is still here: fix the backend and press Retry."
    );
  }
  return `The backend refused the request (HTTP ${status}).`;
}

export type ChatSourceKind = "canned" | "live";

export function chatSourceKind(): ChatSourceKind {
  return process.env.NEXT_PUBLIC_REQTRACE_CHAT_SOURCE === "canned"
    ? "canned"
    : "live";
}

export function pickChatSource(): ChatSource {
  return chatSourceKind() === "live" ? sseSource() : cannedSource();
}
