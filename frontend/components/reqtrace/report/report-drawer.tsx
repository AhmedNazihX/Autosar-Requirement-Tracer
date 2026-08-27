"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  DownloadIcon,
  Loader2Icon,
  PlayIcon,
  RotateCcwIcon,
  XIcon,
} from "lucide-react";

import {
  VERDICT_MEANING,
  VERDICTS,
  type CodeCitation,
  type Verdict,
} from "@/lib/events";
import {
  covered,
  coveredPct,
  exportUrl,
  EXPORT_FORMATS,
  type Coverage,
  type EvidenceItem,
  type ReportResult,
  type ReportRow,
  type ReportScope,
} from "@/lib/reports";
import { useReport } from "@/hooks/use-report";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { UpstreamChip } from "@/components/reqtrace/chat/citation-chips";
import { Cap, Meta } from "@/components/reqtrace/text";

/**
 * The report drawer (feature F5.5) — launch, progress, matrix, export.
 *
 * Three states in one surface, because they are one task: choose a scope and
 * see what it will cost, watch it run, read the matrix. The drawer never
 * unmounts between them, so a run keeps going while the user reads the chat
 * behind it and closing it does not cancel anything.
 *
 * **The cost estimate is shown before the button, not after.** Judging is the
 * only expensive thing this application does, and the number comes from
 * OpenRouter's own published prices (`core/pricing.py`). When it cannot be
 * known the drawer says so and shows the count instead — an invented figure
 * beside a Launch button is worse than no figure.
 *
 * **A row's verdict is never editorialised.** The four statuses are rendered
 * exactly as the judge returned them, `unverifiable` included; spec §5 refused
 * a binary and the table must not quietly reintroduce one by, say, folding
 * `unverifiable` into `missing`.
 */
/**
 * Where the reader had got to in the matrix.
 *
 * Held by the shell, not by the drawer, for the same reason the run is: Base
 * UI unmounts a closed popup, and the drawer now closes every time you follow
 * an evidence span into the code. Without this, coming back put you at the top
 * of 398 rows with your filters cleared — so following a piece of evidence
 * cost you your place, every time.
 *
 * `scrollTopRef` is a ref rather than state on purpose: it changes on every
 * wheel event and nothing needs to re-render when it does.
 */
export interface ReportView {
  filters: Set<Verdict>;
  setFilters: (next: Set<Verdict>) => void;
  scrollTopRef: React.RefObject<number>;
  lastOpened: string | null;
  setLastOpened: (reqId: string) => void;
}

export function useReportView(): ReportView {
  const [filters, setFilters] = useState<Set<Verdict>>(new Set());
  const [lastOpened, setLastOpened] = useState<string | null>(null);
  const scrollTopRef = useRef(0);
  return { filters, setFilters, scrollTopRef, lastOpened, setLastOpened };
}

export function ReportDrawer({
  open,
  onOpenChange,
  onOpenRow,
  defaultModule,
  report,
  view,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * The run, owned by the shell rather than by this component.
   *
   * Base UI unmounts a closed popup, so a hook living here would lose the
   * matrix the moment the drawer closed — and the drawer now closes every time
   * the user follows a row into the source pane. A 398-row run is cheap to
   * repeat but not free to wait for, and losing it on the click that was
   * supposed to show you something is the wrong behaviour twice over.
   */
  report: ReturnType<typeof useReport>;
  /**
   * A row click hands back the requirement id and the code its verdict cited
   * (or `null` when it cited none). The shell resolves the requirement and
   * pins both into the source pane — spec §7's "row click opens both tabs".
   */
  onOpenRow: (
    reqId: string,
    evidence: CodeCitation | null,
    tab: "document" | "code",
  ) => void;
  defaultModule: string;
  view: ReportView;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        // The drawer draws its own close button, in the header row below,
        // aligned with the title and the "New report" action. The primitive's
        // default one is absolutely positioned in the corner, so leaving it on
        // put two X buttons two pixels apart. The source-pane overlay in
        // `app-shell.tsx` turns it off for the same reason.
        showCloseButton={false}
        // The width override has to carry the same `data-[side=right]:`
        // qualifier the base class uses, or it loses on specificity and the
        // drawer silently renders at `max-w-sm` — 384 px, with the verdict and
        // evidence columns off-screen.
        className="flex w-full flex-col gap-0 p-0 data-[side=right]:sm:max-w-[880px]"
      >
        <div className="flex h-12 flex-none items-center gap-2 border-b px-4">
          <SheetTitle className="flex-1 text-[13px] font-semibold tracking-[-0.01em]">
            Traceability report
          </SheetTitle>
          {report.state.phase === "finished" ? (
            <Button variant="ghost" size="xs" onClick={report.reset}>
              <RotateCcwIcon />
              New report
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Close"
            onClick={() => onOpenChange(false)}
          >
            <XIcon />
          </Button>
        </div>

        {report.state.phase === "idle" || report.state.phase === "launching" ? (
          <LaunchForm
            busy={report.state.phase === "launching"}
            defaultModule={defaultModule}
            onLaunch={(scope) => void report.launch(scope)}
          />
        ) : null}

        {report.state.phase === "running" ? (
          <Progress
            label={report.state.launch.scope_label}
            done={report.state.progress.done}
            total={report.state.progress.total || report.state.launch.requirements}
            current={report.state.progress.current}
            estimate={report.state.launch}
            onCancel={report.cancel}
          />
        ) : null}

        {report.state.phase === "finished" ? (
          <Finished
            jobId={report.state.jobId}
            status={report.state.status}
            result={report.state.result}
            error={report.state.error}
            onOpenRow={onOpenRow}
            view={view}
          />
        ) : null}

        {report.state.phase === "failed" ? (
          <div className="flex min-h-0 flex-1 items-start p-6">
            <div className="flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/10 px-3.5 py-3">
              <AlertTriangleIcon className="mt-0.5 size-3.5 flex-none" />
              <div>
                <p className="text-[12.5px] leading-[18px]">{report.state.message}</p>
                <Button
                  variant="outline"
                  size="xs"
                  className="mt-2.5"
                  onClick={report.reset}
                >
                  Start over
                </Button>
              </div>
            </div>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

/* ------------------------------------------------------------- S5.5.1 ----- */

const MODULES = ["Can", "CanIf", "CanTp", "CanSM"] as const;

function LaunchForm({
  busy,
  defaultModule,
  onLaunch,
}: {
  busy: boolean;
  defaultModule: string;
  onLaunch: (scope: ReportScope) => void;
}) {
  const [module, setModule] = useState(defaultModule);
  const [rejudge, setRejudge] = useState(false);
  const [limit, setLimit] = useState("");

  const parsedLimit = limit.trim() ? Number(limit) : null;
  const limitValid = parsedLimit === null || (Number.isInteger(parsedLimit) && parsedLimit > 0);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-6">
      <Cap>Scope</Cap>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {MODULES.map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setModule(name)}
            className={
              name === module
                ? "rounded-md border border-foreground bg-foreground px-2.5 py-1 font-mono text-[11.5px] text-background"
                : "rounded-md border px-2.5 py-1 font-mono text-[11.5px] transition-colors hover:bg-muted"
            }
          >
            {name}
          </button>
        ))}
      </div>
      <p className="mt-2 text-[11.5px] leading-[17px] text-muted-foreground">
        Every requirement in the module is checked against the pinned snapshot.
        {module === "Can" ? (
          <>
            {" "}
            <span className="text-foreground">
              The CAN Driver has no implementation in this repository
            </span>{" "}
            — that scope is the release-drift demonstration, and it will come
            back almost entirely <span className="font-mono">missing</span>.
          </>
        ) : null}
      </p>

      <div className="mt-5 flex flex-col gap-3">
        <label className="flex items-start gap-2.5">
          <input
            type="checkbox"
            checked={rejudge}
            onChange={(event) => setRejudge(event.target.checked)}
            className="mt-0.5 size-3.5 flex-none"
          />
          <span className="text-[12.5px] leading-[17px]">
            Re-judge cached verdicts
            <span className="block text-[11.5px] text-muted-foreground">
              Off by default. Verdicts are cached per requirement, snapshot and
              judge model, so a re-run of an unchanged scope normally costs
              nothing at all.
            </span>
          </span>
        </label>

        <label className="flex items-center gap-2.5">
          <span className="text-[12.5px]">Stop after</span>
          <Input
            value={limit}
            onChange={(event) => setLimit(event.target.value)}
            placeholder="all"
            inputMode="numeric"
            className="h-7 w-24 font-mono text-[11.5px]"
            aria-label="Requirement limit"
          />
          <span className="text-[11.5px] text-muted-foreground">
            requirements — for a quick partial run.
          </span>
        </label>
      </div>

      <div className="mt-6 flex items-center gap-3">
        <Button
          size="sm"
          disabled={busy || !limitValid}
          onClick={() =>
            onLaunch({
              module,
              rejudge,
              limit: parsedLimit,
            })
          }
        >
          {busy ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PlayIcon className="size-3.5" />
          )}
          Estimate and run
        </Button>
        {/* The canvas's rule: a disabled control says why, in text. */}
        {!limitValid ? (
          <span className="text-[11.5px] text-muted-foreground">
            The limit must be a whole number above zero.
          </span>
        ) : (
          <span className="text-[11.5px] text-muted-foreground">
            The cost is estimated first and shown as the run starts.
          </span>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- S5.5.2 ----- */

function Progress({
  label,
  done,
  total,
  current,
  estimate,
  onCancel,
}: {
  label: string;
  done: number;
  total: number;
  current: string;
  estimate: {
    to_judge: number;
    cached: number;
    est_cost_usd: number | null;
    est_basis: string;
    ceiling_usd: number;
  };
  onCancel: () => void;
}) {
  const pct = total ? Math.round((100 * done) / total) : 0;
  return (
    <div className="flex min-h-0 flex-1 flex-col p-6">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[13px] font-medium">{label}</span>
        <Meta>
          {done} / {total || "?"}
        </Meta>
      </div>

      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-foreground transition-[width] duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>

      <p className="mt-2 font-mono text-[11px] text-muted-foreground">
        {current ? `judging ${current}` : "starting…"}
      </p>

      <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-2">
        <Figure label="To judge" value={estimate.to_judge.toLocaleString()} />
        <Figure
          label="From cache"
          value={`${estimate.cached.toLocaleString()} · free`}
        />
        <Figure
          label="Estimated cost"
          value={
            estimate.est_cost_usd === null
              ? "unavailable"
              : `$${estimate.est_cost_usd.toFixed(4)}`
          }
          hint={estimate.est_cost_usd === null ? estimate.est_basis : undefined}
        />
        <Figure label="Hard stop" value={`$${estimate.ceiling_usd.toFixed(2)}`} />
      </dl>

      <p className="mt-4 text-[11.5px] leading-[17px] text-muted-foreground">
        The run continues if you close this drawer, and every verdict is cached
        as it is produced — stopping early keeps what it has already bought.
      </p>

      <div className="mt-5">
        <Button variant="outline" size="sm" onClick={onCancel}>
          Stop the run
        </Button>
      </div>
    </div>
  );
}

function Figure({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div>
      <Cap>{label}</Cap>
      <div className="font-mono text-[12.5px] tabular-nums">{value}</div>
      {hint ? <Meta className="block">{hint}</Meta> : null}
    </div>
  );
}

/* --------------------------------------------------------- S5.5.3/S5.5.4 -- */

const VERDICT_CLASS: Record<Verdict, string> = {
  implemented:
    "border-verdict-implemented-border bg-verdict-implemented-bg text-verdict-implemented",
  partial: "border-verdict-partial-border bg-verdict-partial-bg text-verdict-partial",
  missing: "border-verdict-missing-border bg-verdict-missing-bg text-verdict-missing",
  unverifiable:
    "border-verdict-unverifiable-border bg-verdict-unverifiable-bg text-verdict-unverifiable",
};

function Finished({
  jobId,
  status,
  result,
  error,
  onOpenRow,
  view,
}: {
  jobId: string;
  status: string;
  result: ReportResult | null;
  error: string | null;
  onOpenRow: (
    reqId: string,
    evidence: CodeCitation | null,
    tab: "document" | "code",
  ) => void;
  view: ReportView;
}) {
  const { filters, setFilters, scrollTopRef, lastOpened, setLastOpened } = view;
  const scroller = useRef<HTMLDivElement>(null);

  // Put the reader back where they were, before the browser paints — a visible
  // jump from the top would defeat the point.
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element) element.scrollTop = scrollTopRef.current;
  }, [scrollTopRef]);
  const rows = useMemo(
    () =>
      result
        ? filters.size === 0
          ? result.rows
          : result.rows.filter((row) => filters.has(row.status))
        : [],
    [result, filters],
  );

  if (!result) {
    return (
      <div className="flex min-h-0 flex-1 items-start p-6">
        <p className="text-[12.5px] leading-[18px] text-muted-foreground">
          The run finished as <span className="font-mono">{status}</span> and
          produced no matrix. {error}
        </p>
      </div>
    );
  }

  const { coverage } = result;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex-none border-b px-6 py-4">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-[13px] font-medium">{result.scope_label}</span>
          <Meta>
            ${result.cost_usd.toFixed(4)} · {Math.round(result.elapsed_ms / 1000)}s
          </Meta>
        </div>

        {result.aborted && result.abort_reason ? (
          <div className="mt-3 flex items-start gap-2.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2">
            <AlertTriangleIcon className="mt-0.5 size-3.5 flex-none" />
            <p className="text-[11.5px] leading-[17px]">{result.abort_reason}</p>
          </div>
        ) : null}

        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {VERDICTS.map((verdict) => {
            const count = coverage[verdict];
            const active = filters.has(verdict);
            return (
              <Tooltip key={verdict}>
                <TooltipTrigger
                  render={
              <button
                type="button"
                onClick={() => {
                  const next = new Set(filters);
                  if (next.has(verdict)) next.delete(verdict);
                  else next.add(verdict);
                  setFilters(next);
                  // A filter change reflows the table, so the old offset points
                  // at a different row. Start from the top rather than
                  // somewhere arbitrary.
                  scrollTopRef.current = 0;
                  if (scroller.current) scroller.current.scrollTop = 0;
                }}
                className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11.5px] transition-colors ${
                  VERDICT_CLASS[verdict]
                } ${active ? "ring-2 ring-foreground/40" : "opacity-90 hover:opacity-100"}`}
              >
                {verdict}
                <span className="font-mono tabular-nums">{count}</span>
              </button>
                  }
                />
                <TooltipContent
                  side="bottom"
                  className="max-w-[300px] flex-col items-start gap-1 px-2.5 py-2 text-left"
                >
                  <span className="text-[11.5px] leading-4 font-medium">
                    {verdict}
                  </span>
                  <span className="text-[10.5px] leading-[14.5px] opacity-80">
                    {VERDICT_MEANING[verdict]}
                  </span>
                </TooltipContent>
              </Tooltip>
            );
          })}
          {filters.size > 0 ? (
            <Button variant="ghost" size="xs" onClick={() => setFilters(new Set())}>
              Clear
            </Button>
          ) : null}
        </div>

        <p className="mt-2.5 text-[11.5px] leading-[17px] text-muted-foreground">
          {covered(coverage)} of {coverage.judged} judged have implementation
          evidence ({coveredPct(coverage).toFixed(1)}%). {coverage.from_cache}{" "}
          came from the cache and cost nothing. Judge{" "}
          <span className="font-mono">{result.judge_model_id}</span> against{" "}
          <span className="font-mono">{result.git_sha.slice(0, 7)}</span>.
        </p>

        <div className="mt-3 flex items-center gap-1.5">
          <Cap className="mr-1">Export</Cap>
          {EXPORT_FORMATS.map((fmt) => (
            <a
              key={fmt}
              href={exportUrl(jobId, fmt)}
              download
              className="inline-flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-[11px] transition-colors hover:bg-muted"
            >
              <DownloadIcon className="size-3" />
              {fmt}
            </a>
          ))}
        </div>
      </div>

      <div
        ref={scroller}
        onScroll={(event) => {
          scrollTopRef.current = event.currentTarget.scrollTop;
        }}
        className="min-h-0 flex-1 overflow-auto"
      >
        <table className="w-full border-collapse text-[11.5px]">
          <thead className="sticky top-0 bg-background">
            <tr className="border-b text-left">
              <Th>SRS (upstream)</Th>
              <Th>SWS requirement</Th>
              <Th>Verdict</Th>
              <Th className="text-right">Conf.</Th>
              <Th>Evidence</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Row
                key={row.req_id}
                row={row}
                opened={row.req_id === lastOpened}
                onOpenRow={(reqId, evidence, tab) => {
                  setLastOpened(reqId);
                  onOpenRow(reqId, evidence, tab);
                }}
              />
            ))}
          </tbody>
        </table>
        {rows.length === 0 ? (
          <p className="p-6 text-[12px] text-muted-foreground">
            No rows match that filter.
          </p>
        ) : null}
      </div>
    </div>
  );
}

function Th({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      className={`px-3 py-2 text-[10.5px] font-semibold tracking-[0.09em] uppercase text-muted-foreground ${className}`}
    >
      {children}
    </th>
  );
}

function Row({
  row,
  opened,
  onOpenRow,
}: {
  row: ReportRow;
  /** The row the reader last followed into the source pane. Marked so that
   *  coming back to a 398-row table shows where they left off, rather than
   *  making them find it again. */
  opened: boolean;
  onOpenRow: (
    reqId: string,
    evidence: CodeCitation | null,
    tab: "document" | "code",
  ) => void;
}) {
  return (
    <tr
      className={
        opened
          ? "cursor-pointer border-b border-l-2 border-l-source bg-muted/40 align-top transition-colors hover:bg-muted/60"
          : "cursor-pointer border-b align-top transition-colors hover:bg-muted/60"
      }
      onClick={() =>
        // Both tabs: the requirement's page, and — when the judge cited code —
        // the lines it cited. That pairing is what makes the matrix useful
        // rather than a list of verdicts. The row lands on the requirement;
        // the evidence cell below lands on the code.
        onOpenRow(row.req_id, codeCitationFor(row.evidence[0]), "document")
      }
    >
      <td className="px-3 py-2">
        {row.upstream_ids.length === 0 ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {/* The same chip the chat renders, not a lookalike: an SRS id must
                say the same thing wherever it appears. */}
            {row.upstream_ids.map((id) => (
              <UpstreamChip
                key={id}
                citation={{
                  kind: "upstream",
                  req_id: id,
                  cited_by: row.req_id,
                  doc: upstreamDocument(id),
                }}
              />
            ))}
          </div>
        )}
      </td>
      <td className="px-3 py-2">
        <span className="font-mono">{row.req_id}</span>
        {row.section ? (
          <span className="block text-[10.5px] text-muted-foreground">
            {row.section}
          </span>
        ) : null}
      </td>
      <td className="px-3 py-2">
        {/* `title`, not a Tooltip: this renders once per row and 398 tooltip
            roots is a real cost for a hint the four filter chips above already
            give in full. */}
        <span
          title={`${row.status} — ${VERDICT_MEANING[row.status]}${row.rationale ? `\n\nThe judge said: ${row.rationale}` : ""}`}
          className={`inline-flex cursor-help rounded border px-1.5 py-px text-[10.5px] ${VERDICT_CLASS[row.status]}`}
        >
          {row.status}
        </span>
      </td>
      <td className="px-3 py-2 text-right font-mono tabular-nums">
        {row.confidence.toFixed(2)}
      </td>
      <td className="px-3 py-2">
        {row.evidence.length === 0 ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <div className="flex flex-col items-start gap-1">
            {/* Every cited span is its own control. Showing only the first and
                a "+2" left the rest unreachable — the judge cited them, so the
                reader should be able to open them. */}
            {row.evidence.map((item) => (
              <button
                key={`${item.file}:${item.lines[0]}-${item.lines[1]}`}
                type="button"
                title={item.rationale}
                onClick={(event) => {
                  // Without this the row handler fires too and lands on the
                  // document tab — the opposite of what was clicked.
                  event.stopPropagation();
                  onOpenRow(row.req_id, codeCitationFor(item), "code");
                }}
                className="rounded border border-source-border bg-source-bg px-1.5 py-px font-mono text-[10.5px] text-source transition-colors hover:brightness-110"
              >
                {item.file.split("/").pop()}:{item.lines[0]}–{item.lines[1]}
              </button>
            ))}
          </div>
        )}
      </td>
    </tr>
  );
}

/** An evidence item as the citation the source pane opens. */
function codeCitationFor(item: EvidenceItem | undefined): CodeCitation | null {
  return item
    ? {
        kind: "code",
        repo_path: item.file,
        symbol: item.symbol,
        line_span: item.lines,
        git_sha: item.git_sha,
      }
    : null;
}

/** A display name for an SRS document, derived from its id — the same rule
 *  `agent/tools.py::_upstream_document` uses, because the chip shows it. */
function upstreamDocument(upstreamId: string): string {
  const parts = upstreamId.split("_");
  return parts.length >= 2 ? parts.slice(0, 2).join(" ") : upstreamId;
}

export type { Coverage };
