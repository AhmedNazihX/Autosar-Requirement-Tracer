/**
 * The traceability-report contract, and the only place the report API is called.
 *
 * `lib/events.ts` is the authority on the **chat** SSE envelope and says not to
 * fork it. Reports are a second, separate contract — `POST /reports` and
 * `GET /reports/{id}/events` — so they live here rather than being bolted onto
 * that file, and `api/reports.py` is the Python side. Its module docstring says
 * plainly: *"there is no report contract there yet, so the frames are defined
 * here as Pydantic models and WP5 must match them rather than invent a second
 * shape."* This is that match. Every interface below mirrors a Pydantic model
 * in `api/reports.py` or `engines/report.py`, field for field.
 *
 * **Two derived numbers are computed here, not received.** `Coverage.covered`
 * and `covered_pct` are Python `@property`s, and `model_dump()` does not
 * serialise properties — so they are absent from the wire and are recomputed
 * by :func:`covered` / :func:`coveredPct`. Reading them off the payload would
 * silently give `undefined`.
 */

import type { Verdict } from "./events";

const API = "/api/py/reports";

/** Mirrors `engines.report.ReportScope`. */
export interface ReportScope {
  module?: string | null;
  /** Several modules at once — the union, in document order. Wins over `module`. */
  modules?: string[] | null;
  document?: string | null;
  req_ids?: string[] | null;
  rejudge?: boolean;
  limit?: number | null;
}

/** Mirrors `engines.report.EvidenceItem`. */
export interface EvidenceItem {
  file: string;
  lines: [number, number];
  rationale: string;
  symbol: string;
  git_sha: string;
}

/** Mirrors `engines.report.ReportRow` — one row of the matrix. */
export interface ReportRow {
  req_id: string;
  /** The requirement's own words — the matrix is read without the PDF open. */
  text: string;
  doc: string;
  module: string;
  section: string | null;
  page: number;
  /** The SRS side of SRS → SWS → code. Not ingested, so never clickable. */
  upstream_ids: string[];
  status: Verdict;
  confidence: number;
  rationale: string;
  evidence: EvidenceItem[];
  cached: boolean;
}

/** Mirrors `engines.report.Coverage`. No `covered`/`covered_pct` — see above. */
export interface Coverage {
  total: number;
  judged: number;
  from_cache: number;
  implemented: number;
  partial: number;
  missing: number;
  unverifiable: number;
  with_evidence: number;
}

/** Mirrors `engines.report.ReportResult`. */
export interface ReportResult {
  project_id: string;
  scope: ReportScope;
  scope_label: string;
  git_sha: string;
  judge_model_id: string;
  corpus_version: string;
  rows: ReportRow[];
  coverage: Coverage;
  cost_usd: number;
  ceiling_usd: number;
  elapsed_ms: number;
  unknown_ids: string[];
  aborted: boolean;
  abort_reason: string | null;
}

export type RunStatus =
  | "running"
  | "succeeded"
  | "aborted"
  | "failed"
  | "cancelled";

/** Mirrors `api.reports.RunView`. */
export interface RunView {
  id: string;
  status: RunStatus;
  scope: ReportScope;
  scope_label: string;
  done: number;
  total: number;
  current: string | null;
  est_cost_usd: number | null;
  est_basis: string;
  cost_usd: number;
  ceiling_usd: number;
  error: string | null;
  result: ReportResult | null;
}

/**
 * Mirrors `api.reports.EstimateResponse` — the launch dialog's figures with
 * no job behind them, from `POST /reports/estimate`.
 */
export interface EstimateResponse {
  scope_label: string;
  requirements: number;
  to_judge: number;
  cached: number;
  est_cost_usd: number | null;
  est_basis: string;
  ceiling_usd: number;
  unknown_ids: string[];
}

/** Mirrors `api.reports.LaunchResponse` — the estimate plus the started job. */
export interface LaunchResponse extends EstimateResponse {
  job_id: string;
  status: RunStatus;
}

/**
 * Mirrors `api.reports.ProgressData` — spec §6's `{done, total, current}`,
 * plus the running per-status tallies and the spend so far.
 */
export interface ProgressData {
  done: number;
  total: number;
  current: string;
  implemented: number;
  partial: number;
  missing: number;
  unverifiable: number;
  cost_usd: number;
}

export type ReportFrame =
  | { type: "progress"; data: ProgressData }
  | { type: "done"; data: { status: RunStatus; [key: string]: unknown } }
  | { type: "error"; data: { message: string } };

export const EXPORT_FORMATS = ["md", "csv", "json"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

/* --------------------------------------------------------------- derived -- */

/** Requirements with any implementation evidence at all. */
export function covered(coverage: Coverage): number {
  return coverage.implemented + coverage.partial;
}

export function coveredPct(coverage: Coverage): number {
  return coverage.judged ? (100 * covered(coverage)) / coverage.judged : 0;
}

/* ----------------------------------------------------------------- calls -- */

export class ReportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReportError";
  }
}

async function readError(response: Response, fallback: string): Promise<string> {
  // FastAPI puts the sentence in `detail`, and those sentences are written to
  // be shown — "no module 'Ethernet' in this corpus (known: …)" is more use to
  // the user than the status code that carried it.
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* fall through */
  }
  return fallback;
}

export async function launchReport(scope: ReportScope): Promise<LaunchResponse> {
  const response = await fetch(API, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ scope }),
  });
  if (!response.ok) {
    throw new ReportError(
      await readError(response, `The report could not be started (HTTP ${response.status}).`),
    );
  }
  return (await response.json()) as LaunchResponse;
}

export async function estimateReport(scope: ReportScope): Promise<EstimateResponse> {
  const response = await fetch(`${API}/estimate`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ scope }),
  });
  if (!response.ok) {
    throw new ReportError(
      await readError(response, `The scope could not be estimated (HTTP ${response.status}).`),
    );
  }
  return (await response.json()) as EstimateResponse;
}

export async function getRun(jobId: string): Promise<RunView | null> {
  const response = await fetch(`${API}/${encodeURIComponent(jobId)}`);
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new ReportError(
      await readError(response, `Could not read report ${jobId}.`),
    );
  }
  return (await response.json()) as RunView;
}

export async function cancelRun(jobId: string): Promise<void> {
  await fetch(`${API}/${encodeURIComponent(jobId)}`, { method: "DELETE" });
}

export function exportUrl(jobId: string, fmt: ExportFormat): string {
  return `${API}/${encodeURIComponent(jobId)}/export?fmt=${fmt}`;
}

export function eventsUrl(jobId: string): string {
  return `${API}/${encodeURIComponent(jobId)}/events`;
}

export function isReportFrame(value: unknown): value is ReportFrame {
  if (typeof value !== "object" || value === null) return false;
  const frame = value as { type?: unknown; data?: unknown };
  return (
    (frame.type === "progress" || frame.type === "done" || frame.type === "error") &&
    typeof frame.data === "object" &&
    frame.data !== null
  );
}
