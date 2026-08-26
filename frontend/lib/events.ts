/**
 * The chat SSE event model — spec §6.
 *
 * This is the ONE place the wire format is described. Every piece of chat UI
 * state (streamed text, tool chips, citation chips, cost line, error banners)
 * is derived from a stream of these events by `lib/event-reducer.ts`. Nothing
 * is parsed out of prose, and nothing is computed ad hoc in a component.
 *
 * The backend's `POST /chat` SSE envelope must match this file. When WP3 lands,
 * check the FastAPI response models against these types — do not fork them.
 */

/** The five tools the chat agent may call (spec §3). */
export const TOOL_NAMES = [
  "lookup_requirement",
  "search_requirements",
  "search_code",
  "check_implementation",
  "generate_traceability_report",
] as const;

export type ToolName = (typeof TOOL_NAMES)[number];

/** The four verdicts the evidence judge may return (spec §5). Never binary. */
export const VERDICTS = [
  "implemented",
  "partial",
  "missing",
  "unverifiable",
] as const;

export type Verdict = (typeof VERDICTS)[number];

/**
 * One stage of the advanced RAG pipeline (spec §4), reported by
 * `search_requirements` so the pipeline is visible without a debug panel.
 */
export interface RagStage {
  /** 1-based stage index. */
  step: number;
  /** `multi-query`, `self-query`, `hybrid + RRF`, `rerank`. */
  label: string;
  /** Free-form counts, e.g. `bm25 42 · dense 40 → fused 20`. */
  detail: string;
}

/** An incremental text delta for the assistant message. */
export interface TokenEventData {
  text: string;
}

/** A tool call has started. Renders as a pending chip. */
export interface ToolStartEventData {
  /** Correlates with the matching `tool_result`. */
  id: string;
  tool: ToolName;
  /** Verbatim tool arguments. Rendered as data, never interpreted. */
  args: Record<string, unknown>;
}

/**
 * The outcome of a specific `tool_start`, correlated by `id`.
 *
 * Ordering is not guaranteed and a `tool_start` may never get a result — a
 * stream can end mid-flight. The reducer reports that honestly rather than
 * leaving a chip spinning forever.
 */
export interface ToolResultEventData {
  id: string;
  status: "ok" | "error";
  /** One short line for the chip, e.g. `5 of 20` or `SWS_Can_00011`. */
  summary: string;
  duration_ms: number;
  /** Present when `status === "error"`. User-readable, never a stack trace. */
  error?: string;
  /** Only `search_requirements` reports these. */
  stages?: RagStage[];
}

/**
 * A structured pointer into a source document.
 *
 * `bbox` is `null` when the requirement's text crosses a page boundary; the
 * source pane says so rather than drawing a wrong rectangle (canvas artboard 2).
 */
export interface RequirementCitation {
  kind: "requirement";
  /** Exactly as the corpus spells it. NEVER normalise the casing. */
  req_id: string;
  /** Document id, e.g. `AUTOSAR_CP_SWS_CANInterface`. */
  doc: string;
  doc_title: string;
  page: number;
  bbox: [number, number, number, number] | null;
  /** Set when the requirement spans pages and `bbox` is therefore null. */
  page_span?: [number, number];
  page_count: number;
  section?: string;
  /** Verbatim requirement text, as extracted. Untrusted input. */
  quote?: string;
}

/** A structured pointer into the indexed code snapshot. */
export interface CodeCitation {
  kind: "code";
  /** Path relative to the repo root, inside the indexed snapshot only. */
  repo_path: string;
  symbol: string;
  line_span: [number, number];
  /** The pinned SHA the span is only true for. */
  git_sha: string;
}

/**
 * An upstream (SRS/RS) requirement cited by an SWS requirement.
 *
 * SRS documents are not in the index, so these render as dashed, tooltipped
 * chips and are NEVER clickable through to a document (spec §11).
 */
export interface UpstreamCitation {
  kind: "upstream";
  req_id: string;
  /** The SWS requirement that cites it. */
  cited_by: string;
  doc: string;
}

export type Citation = RequirementCitation | CodeCitation | UpstreamCitation;

/** Tokens and cost for the message; renders as the per-message cost line. */
export interface UsageEventData {
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  elapsed_ms: number;
}

/** Terminal success. */
export interface DoneEventData {
  finish_reason?: string;
}

/**
 * A user-readable failure. Renders inline AND as a toast, and must leave the
 * thread consistent rather than half-rendered — whatever streamed before the
 * error is kept.
 */
export interface ErrorEventData {
  message: string;
  /** Machine-readable code, for logs. Never shown raw to the user. */
  code?: string;
  retryable?: boolean;
}

export type ChatEvent =
  | { type: "token"; data: TokenEventData }
  | { type: "tool_start"; data: ToolStartEventData }
  | { type: "tool_result"; data: ToolResultEventData }
  | { type: "citation"; data: Citation }
  | { type: "usage"; data: UsageEventData }
  | { type: "done"; data: DoneEventData }
  | { type: "error"; data: ErrorEventData };

export type ChatEventType = ChatEvent["type"];

/** Type guard used by the SSE source to reject anything off-contract. */
export function isChatEvent(value: unknown): value is ChatEvent {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { type?: unknown; data?: unknown };
  if (typeof candidate.type !== "string") return false;
  if (typeof candidate.data !== "object" || candidate.data === null)
    return false;
  return (
    candidate.type === "token" ||
    candidate.type === "tool_start" ||
    candidate.type === "tool_result" ||
    candidate.type === "citation" ||
    candidate.type === "usage" ||
    candidate.type === "done" ||
    candidate.type === "error"
  );
}
