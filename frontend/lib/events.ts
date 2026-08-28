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
 * What each verdict means, in the reader's terms.
 *
 * These paraphrase the definitions the judge itself is given — the four
 * bullets in `backend/engines/evidence.py`'s `SYSTEM_PROMPT` — and they are
 * kept beside `VERDICTS` so a fifth verdict, or a changed definition, is
 * obviously incomplete here. If the two ever disagree, the prompt is what the
 * model actually followed and this is the stale copy.
 *
 * Each also states the consequence, because that is the part a reader cannot
 * infer from the label: `implemented` and `partial` are what the coverage
 * percentage counts, and `unverifiable` is deliberately counted as neither.
 */
export const VERDICT_MEANING: Record<Verdict, string> = {
  implemented:
    "A cited code unit clearly performs what the requirement mandates. Counts towards coverage.",
  partial:
    "Some of the mandated behaviour is present, or it is present only for some of the cases the requirement covers. Counts towards coverage.",
  missing:
    "The candidates were relevant enough to judge, and none of them implements the requirement. Usually release drift — the snapshot implements an older AUTOSAR release — rather than a defect.",
  unverifiable:
    "The evidence did not settle it: the candidates were unrelated, or the requirement is about configuration, documentation or naming that source code cannot decide. Counted as neither implemented nor missing, on purpose.",
};

/**
 * One stage of the advanced RAG pipeline (spec §4), reported by
 * `search_requirements` and `search_code` so the pipeline is visible without
 * a debug panel — and by `check_implementation`, whose evidence tiers
 * (annotation scan, symbol anchors, semantic fill, judge) travel through the
 * same log.
 *
 * Everything below `detail` is optional and feeds the chip's funnel
 * rendering; a stage that carries none of it renders as the plain text row
 * it always was, which is what keeps threads stored before these fields
 * existed replaying unchanged.
 */
export interface RagStage {
  /** 1-based stage index. */
  step: number;
  /** `multi-query`, `self-query`, `hybrid search`, `RRF fusion`, `LLM rerank`. */
  label: string;
  /** Free-form counts, e.g. `bm25 42 · dense 40 → fused 20`. */
  detail: string;
  /** Candidates flowing out of the stage — what the funnel bar is drawn from. */
  count?: number;
  /** `multi-query` only: the queries actually searched, the original first. */
  queries?: string[];
  /** `self-query` only: the filters applied, formatted `module=CanSM`. */
  filters?: string[];
  /** Filters that could not be used, with the reason, or hybrid search's
   *  "filter matched nothing — retried unfiltered" fallback. */
  dropped_filters?: string[];
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
  /** `search_requirements` and `search_code` report the retrieval pipeline;
   *  `check_implementation` reports its evidence tiers and the judge through
   *  the same log. */
  stages?: RagStage[];
  /**
   * Only `generate_traceability_report` reports this: the launched run's id.
   * The shell hands it to the report drawer (`useReport().attach`), which is
   * how a chat-started report shows progress there — structured, because
   * prose is never parsed (spec §6).
   */
  job_id?: string;
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
  /**
   * The document's **manifest key** — `can_interface`, never a filename and
   * never a PDF title. One identity end to end: it is what ingestion stores
   * as `Requirement.source_doc` and what `GET /documents/{doc}/view` (spec
   * §6) takes. `doc_title` is the human-readable name.
   */
  doc: string;
  doc_title: string;
  page: number;
  bbox: [number, number, number, number] | null;
  /**
   * Set when the requirement spans pages and `bbox` is therefore null.
   *
   * NOTE for WP3: the backend cannot populate this from storage yet.
   * `Requirement` records only the *start* page (`core/models.py`), so there
   * is no end page to send. Acceptable under ruling R21 — but widen the model
   * before wiring this, rather than hunting for a field that was never
   * stored. Until then only the canned fixture can carry it.
   */
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
  /**
   * The upstream document's name, for display only. Not a manifest key:
   * SRS documents are not in the corpus, so there is nothing to key on and
   * nothing to open.
   */
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
