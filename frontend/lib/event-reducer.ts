/**
 * events[] -> render state.
 *
 * PURE and dependency-free on purpose: no React, no DOM, no imports beyond the
 * event types. Every visible property of an assistant message is a function of
 * its stored event array and nothing else, which is what makes thread reload an
 * exact replay (spec §11) and what makes this module the first thing a later
 * story can unit-test without a browser or a network.
 *
 * Call it with `live: true` while a stream is open and `live: false` for stored
 * events. That single flag is the difference between "still waiting" and
 * "never arrived", and it is why a killed stream cannot leave a chip spinning.
 */

import type {
  ChatEvent,
  Citation,
  CodeCitation,
  ErrorEventData,
  RagStage,
  RequirementCitation,
  ToolName,
  UpstreamCitation,
  UsageEventData,
} from "./events";

export type ToolCallStatus = "pending" | "ok" | "error" | "unresolved";

export interface ToolCallState {
  id: string;
  tool: ToolName;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  summary: string | null;
  durationMs: number | null;
  error: string | null;
  stages: RagStage[] | null;
}

export type MessageStatus = "streaming" | "complete" | "error" | "interrupted";

export interface MessageRenderState {
  /** Concatenated `token` deltas. Plain text; paragraphs split on blank lines. */
  text: string;
  /** In `tool_start` order — the first one drives the checkpoint rail icon. */
  toolCalls: ToolCallState[];
  /** Clickable pointers into the source pane. */
  sources: (RequirementCitation | CodeCitation)[];
  /** Never clickable: SRS/RS documents are not ingested. */
  upstream: UpstreamCitation[];
  usage: UsageEventData | null;
  error: ErrorEventData | null;
  status: MessageStatus;
}

export interface ReduceOptions {
  /**
   * True while the stream is still open. Unresolved tool calls read as
   * `pending` when live and `unresolved` once the stream is closed.
   */
  live?: boolean;
}

export function reduceEvents(
  events: readonly ChatEvent[],
  options: ReduceOptions = {},
): MessageRenderState {
  const live = options.live ?? false;

  const chunks: string[] = [];
  const toolCalls: ToolCallState[] = [];
  const indexById = new Map<string, number>();
  const sources: (RequirementCitation | CodeCitation)[] = [];
  const upstream: UpstreamCitation[] = [];
  const citationKeys = new Set<string>();
  let usage: UsageEventData | null = null;
  let error: ErrorEventData | null = null;
  let done = false;

  for (const event of events) {
    switch (event.type) {
      case "token": {
        chunks.push(event.data.text);
        break;
      }
      case "tool_start": {
        // A duplicate id would silently overwrite a chip; keep the first.
        if (indexById.has(event.data.id)) break;
        indexById.set(event.data.id, toolCalls.length);
        toolCalls.push({
          id: event.data.id,
          tool: event.data.tool,
          args: event.data.args,
          status: "pending",
          summary: null,
          durationMs: null,
          error: null,
          stages: null,
        });
        break;
      }
      case "tool_result": {
        const index = indexById.get(event.data.id);
        // A result with no matching start is dropped rather than invented.
        if (index === undefined) break;
        const call = toolCalls[index];
        toolCalls[index] = {
          ...call,
          status: event.data.status,
          summary: event.data.summary,
          durationMs: event.data.duration_ms,
          error: event.data.error ?? null,
          stages: event.data.stages ?? null,
        };
        break;
      }
      case "citation": {
        // Deduplicated, and not only to silence React's duplicate-key warning.
        // One turn legitimately cites the same requirement from two tools —
        // `search_requirements` finds it and `check_implementation` then
        // reports on it — and the second chip points at exactly the same page
        // as the first. Rendering both is noise; two React children with the
        // same key is also unsupported behaviour.
        //
        // First occurrence wins, which is the earliest tool call, which is the
        // order the reader saw the answer built in.
        const key = citationKey(event.data);
        if (citationKeys.has(key)) break;
        citationKeys.add(key);
        if (event.data.kind === "upstream") upstream.push(event.data);
        else sources.push(event.data);
        break;
      }
      case "usage": {
        usage = event.data;
        break;
      }
      case "done": {
        done = true;
        break;
      }
      case "error": {
        // First error wins: it is the one that stopped the stream.
        error ??= event.data;
        break;
      }
    }
  }

  const status: MessageStatus = error
    ? "error"
    : done
      ? "complete"
      : live
        ? "streaming"
        : "interrupted";

  // Once the stream is closed, a start with no result never got one. Saying so
  // is the whole point — a chip that stays "pending" forever is a lie.
  const settled =
    status === "streaming"
      ? toolCalls
      : toolCalls.map((call) =>
          call.status === "pending" ? { ...call, status: "unresolved" } : call,
        );

  return {
    text: chunks.join(""),
    toolCalls: settled as ToolCallState[],
    sources,
    upstream,
    usage,
    error,
    status,
  };
}

/**
 * The checkpoint rail's icon for an exchange comes from the first `tool_start`
 * in it (canvas artboard 6). A turn that called no tool gets `null`.
 */
export function firstToolOf(events: readonly ChatEvent[]): ToolName | null {
  for (const event of events) {
    if (event.type === "tool_start") return event.data.tool;
  }
  return null;
}

/**
 * What makes two citations the same source.
 *
 * Mirrors the keys `citation-chips.tsx` renders with, deliberately: if these
 * two ever disagree, the chips get duplicate React keys again and the symptom
 * is a console warning nobody reads rather than a test failure. Requirements
 * are identified by document and id (an id is only unique within its
 * document), code by path and line span, and an upstream reference by the pair
 * — the same SRS id cited by two different requirements is two distinct facts.
 */
function citationKey(citation: Citation): string {
  switch (citation.kind) {
    case "code":
      return `code:${citation.repo_path}:${citation.line_span[0]}-${citation.line_span[1]}`;
    case "upstream":
      return `upstream:${citation.req_id}:${citation.cited_by}`;
    default:
      return `requirement:${citation.doc}:${citation.req_id}`;
  }
}
