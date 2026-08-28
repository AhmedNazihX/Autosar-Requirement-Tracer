"use client";

import { useLayoutEffect, useMemo, useRef } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  CircleHelpIcon,
  DownloadIcon,
  InfoIcon,
  LayersIcon,
  SearchIcon,
  TriangleAlertIcon,
} from "lucide-react";

import {
  VERDICT_MEANING,
  VERDICTS,
  type CodeCitation,
} from "@/lib/events";
import {
  exportUrl,
  EXPORT_FORMATS,
  type EvidenceItem,
  type ReportResult,
  type ReportRow,
} from "@/lib/reports";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { UpstreamChip } from "@/components/reqtrace/chat/citation-chips";
import { Cap, Meta } from "@/components/reqtrace/text";
import type { ReportView } from "./report-drawer";
import {
  tallyLine,
  VERDICT_CLASS,
  VerdictBar,
  type VerdictCounts,
} from "./verdicts";

/**
 * The finished matrix (stories S5.5.3/S5.5.4) — SRS → SWS → verdict → evidence,
 * with the drift finding stated before the first row.
 *
 * **A row's verdict is never editorialised.** The four statuses are rendered
 * exactly as the judge returned them, `unverifiable` included; spec §5 refused
 * a binary and the table must not quietly reintroduce one by, say, folding
 * `unverifiable` into `missing`.
 *
 * **Missing rows collapse per module by default.** In this corpus `missing`
 * is the majority outcome and the expected one (release drift), so 231
 * near-identical rows would bury the 9 that carry evidence. The collapse is
 * suspended whenever the reader has asked to see missing rows — by activating
 * the missing filter chip or by typing a search — because hiding a row someone
 * asked for is worse than a long list.
 */

/** One shared grid so the header, group rows and requirement rows line up. */
const GRID =
  "grid grid-cols-[minmax(200px,3fr)_minmax(110px,2fr)_100px_44px_minmax(220px,4fr)_20px] items-start gap-x-3 px-4";

/** Requirement rows rendered per module before "Show more" — the design's
 *  "Rows 50". Client-side only; the data is already here. */
const PAGE_SIZE = 50;

interface Group {
  module: string;
  doc: string;
  rows: ReportRow[];
  counts: VerdictCounts;
}

/** Group rows by module, preserving the report's own order. */
function groupByModule(rows: readonly ReportRow[]): Group[] {
  const byModule = new Map<string, Group>();
  for (const row of rows) {
    let group = byModule.get(row.module);
    if (!group) {
      group = {
        module: row.module,
        doc: row.doc,
        rows: [],
        counts: { implemented: 0, partial: 0, missing: 0, unverifiable: 0 },
      };
      byModule.set(row.module, group);
    }
    group.rows.push(row);
    group.counts[row.status] += 1;
  }
  return [...byModule.values()];
}

function matchesQuery(row: ReportRow, needle: string): boolean {
  if (
    row.req_id.toLowerCase().includes(needle) ||
    row.text.toLowerCase().includes(needle) ||
    row.rationale.toLowerCase().includes(needle)
  )
    return true;
  return row.evidence.some(
    (item) =>
      item.file.toLowerCase().includes(needle) ||
      item.symbol.toLowerCase().includes(needle),
  );
}

export function Matrix({
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
  const {
    filters,
    setFilters,
    query,
    setQuery,
    collapsedGroups,
    setCollapsedGroups,
    expandedMissing,
    setExpandedMissing,
    pageSizes,
    setPageSizes,
    scrollTopRef,
    lastOpened,
    setLastOpened,
  } = view;
  const scroller = useRef<HTMLDivElement>(null);

  // Put the reader back where they were, before the browser paints — a visible
  // jump from the top would defeat the point.
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element) element.scrollTop = scrollTopRef.current;
  }, [scrollTopRef]);

  // A filter or search change reflows the table, so the old offset points at a
  // different row. Start from the top rather than somewhere arbitrary.
  const resetScroll = () => {
    scrollTopRef.current = 0;
    if (scroller.current) scroller.current.scrollTop = 0;
  };

  const needle = query.trim().toLowerCase();

  const visibleGroups = useMemo(() => {
    if (!result) return [];
    let rows = result.rows;
    if (needle) rows = rows.filter((row) => matchesQuery(row, needle));
    if (filters.size > 0) rows = rows.filter((row) => filters.has(row.status));
    return groupByModule(rows);
  }, [result, needle, filters]);

  // The breakdown card describes the report, not the current filter — its
  // numbers must keep matching the summary tiles above it.
  const moduleSummaries = useMemo(
    () => (result ? groupByModule(result.rows) : []),
    [result],
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
  const pctOfTotal = (count: number) =>
    coverage.total ? (100 * count) / coverage.total : 0;

  // The missing collapse is suspended when the reader asked for missing rows.
  const collapseMissing = filters.size === 0 && needle === "";

  const sections = visibleGroups.map((group) => {
    const collapsed = collapsedGroups.has(group.module);
    const missingRows = group.rows.filter((row) => row.status === "missing");
    const summarise = collapseMissing && missingRows.length > 0;
    const expanded = expandedMissing.has(group.module);
    const listRows =
      summarise && !expanded
        ? group.rows.filter((row) => row.status !== "missing")
        : group.rows;
    const pageSize = pageSizes[group.module] ?? PAGE_SIZE;
    const rendered = collapsed ? [] : listRows.slice(0, pageSize);
    return {
      group,
      collapsed,
      missingRows,
      summarise,
      expanded,
      rendered,
      truncated: collapsed ? 0 : listRows.length - rendered.length,
    };
  });
  const shownRows = sections.reduce((sum, s) => sum + s.rendered.length, 0);

  const open = (reqId: string, evidence: CodeCitation | null, tab: "document" | "code") => {
    setLastOpened(reqId);
    onOpenRow(reqId, evidence, tab);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        ref={scroller}
        onScroll={(event) => {
          scrollTopRef.current = event.currentTarget.scrollTop;
        }}
        className="min-h-0 flex-1 overflow-auto"
      >
        <div className="flex flex-col gap-3.5 px-5 py-4">
          <div className="flex items-start gap-3">
            <Meta className="min-w-0 flex-1 text-[11px] leading-4">
              {result.scope_label} · {result.git_sha.slice(0, 7)} ·{" "}
              {result.judge_model_id} · {coverage.total} requirements · $
              {result.cost_usd.toFixed(2)} · {Math.round(result.elapsed_ms / 1000)}s
            </Meta>
            <div className="flex flex-none items-center">
              {EXPORT_FORMATS.map((fmt, index) => (
                <a
                  key={fmt}
                  href={exportUrl(jobId, fmt)}
                  download
                  className={`inline-flex h-6 items-center gap-1 border bg-background px-2 font-mono text-[11px] transition-colors hover:bg-muted ${
                    index === 0
                      ? "rounded-l-md"
                      : index === EXPORT_FORMATS.length - 1
                        ? "-ml-px rounded-r-md"
                        : "-ml-px"
                  }`}
                >
                  {index === 0 ? <DownloadIcon className="size-3" /> : null}
                  {fmt}
                </a>
              ))}
            </div>
          </div>

          {result.aborted && result.abort_reason ? (
            // Partial tokens, not destructive: an aborted run is a warning
            // about incompleteness — the verdicts it did buy are all kept.
            <div className="flex items-start gap-2.5 rounded-lg border border-verdict-partial-border bg-verdict-partial-bg px-3 py-2 text-verdict-partial">
              <TriangleAlertIcon className="mt-0.5 size-3.5 flex-none" />
              <p className="text-[11.5px] leading-[17px]">{result.abort_reason}</p>
            </div>
          ) : null}

          <div className="flex items-start gap-2.5 rounded-[10px] border bg-muted px-3.5 py-3">
            <InfoIcon className="mt-0.5 size-4 flex-none text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] font-semibold">
                Expected release drift — the code predates the specification
              </p>
              <p className="mt-1 text-[11.5px] leading-[16.5px] text-muted-foreground">
                The permitted repository{" "}
                <span className="font-mono">openAUTOSAR/classic-platform</span>{" "}
                implements the AUTOSAR 4.0-era CAN modules; the ingested
                specifications are R23-11. A{" "}
                <span className="text-foreground">missing</span> verdict
                therefore means <em>this snapshot carries no evidence for this
                requirement</em> — not that the requirement is unmet in general,
                and not that the tool failed. {coverage.missing} of the{" "}
                {coverage.total} requirements in scope (
                {pctOfTotal(coverage.missing).toFixed(1)}%) came back missing.
              </p>
            </div>
            <Popover>
              <PopoverTrigger
                render={
                  <Button variant="outline" size="xs" className="mt-0.5 flex-none">
                    <CircleHelpIcon className="size-3" />
                    How verdicts are decided
                  </Button>
                }
              />
              <PopoverContent side="bottom" align="end" className="w-[360px]">
                <div className="flex flex-col gap-2.5">
                  {VERDICTS.map((verdict) => (
                    <div key={verdict}>
                      <span
                        className={`inline-flex rounded border px-1.5 py-px text-[10.5px] font-medium ${VERDICT_CLASS[verdict]}`}
                      >
                        {verdict}
                      </span>
                      <p className="mt-1 text-[10.5px] leading-[14.5px] text-muted-foreground">
                        {VERDICT_MEANING[verdict]}
                      </p>
                    </div>
                  ))}
                  <p className="border-t pt-2 text-[10.5px] leading-[14.5px] text-muted-foreground">
                    Each verdict rests on three tiers of evidence, strongest
                    first: explicit <span className="font-mono">@req</span>/
                    <span className="font-mono">!req</span> annotations in the
                    code, the symbols the requirement names, and semantic
                    candidates from the index.
                  </p>
                </div>
              </PopoverContent>
            </Popover>
          </div>

          <div className="flex items-stretch gap-2.5">
            {VERDICTS.map((verdict) => (
              <div
                key={verdict}
                className={`flex-1 rounded-[10px] border px-3 py-2.5 ${VERDICT_CLASS[verdict]}`}
              >
                <div className="text-[11.5px] font-medium">{verdict}</div>
                <div className="mt-1 flex items-baseline gap-1.5">
                  <span className="font-mono text-[22px] leading-6 font-semibold">
                    {coverage[verdict]}
                  </span>
                  <span className="font-mono text-[11px] opacity-70">
                    {pctOfTotal(coverage[verdict]).toFixed(1)}%
                  </span>
                </div>
              </div>
            ))}
          </div>

          <VerdictBar counts={coverage} total={coverage.total} className="h-2" />

          <div className="rounded-[10px] bg-muted px-3.5 py-1">
            {moduleSummaries.map((group, index) => (
              <div key={group.module}>
                {index > 0 ? <div className="h-px bg-border" /> : null}
                <div className="py-2">
                  <div className="mb-1.5 flex items-baseline gap-2">
                    <span className="w-14 flex-none font-mono text-[11.5px] font-medium">
                      {group.module}
                    </span>
                    <span className="truncate text-[11.5px]">{group.doc}</span>
                    <Meta className="flex-1 text-[11px]">
                      {group.rows.length} requirements
                    </Meta>
                    <Meta className="text-[11px]">{tallyLine(group.counts)}</Meta>
                  </div>
                  <VerdictBar
                    counts={group.counts}
                    total={group.rows.length}
                    className="h-1.5"
                  />
                </div>
              </div>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-[260px] flex-none">
              <SearchIcon className="absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  resetScroll();
                }}
                placeholder="Filter by ID, symbol or text"
                aria-label="Filter rows"
                className="h-8 pl-8 font-mono text-[11.5px]"
              />
            </div>
            {VERDICTS.map((verdict) => {
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
                          resetScroll();
                        }}
                        className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11.5px] font-medium transition-colors ${
                          VERDICT_CLASS[verdict]
                        } ${active ? "ring-2 ring-foreground/40" : "opacity-90 hover:opacity-100"}`}
                      >
                        {verdict}
                        <span className="font-mono text-[10.5px] tabular-nums opacity-80">
                          {coverage[verdict]}
                        </span>
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
              <Button
                variant="ghost"
                size="xs"
                onClick={() => {
                  setFilters(new Set());
                  resetScroll();
                }}
              >
                Clear
              </Button>
            ) : null}
            <span className="ml-auto flex h-6 flex-none items-center gap-1.5 rounded-md border bg-muted px-2 text-[11px] text-muted-foreground">
              <LayersIcon className="size-3" />
              grouped by module
            </span>
          </div>
        </div>

        <div className={`${GRID} sticky top-0 z-10 border-y bg-muted py-1.5`}>
          <Cap>SWS requirement</Cap>
          <Cap>Upstream</Cap>
          <Cap>Verdict</Cap>
          <Cap className="text-right">Conf.</Cap>
          <Cap>Evidence &amp; rationale</Cap>
          <span />
        </div>

        {sections.map(
          ({ group, collapsed, missingRows, summarise, expanded, rendered, truncated }) => (
            <div key={group.module}>
              <button
                type="button"
                onClick={() => {
                  const next = new Set(collapsedGroups);
                  if (next.has(group.module)) next.delete(group.module);
                  else next.add(group.module);
                  setCollapsedGroups(next);
                }}
                className="flex w-full items-center gap-2 border-b bg-muted px-4 py-2 text-left transition-colors hover:bg-accent"
              >
                <ChevronDownIcon
                  className={`size-3.5 flex-none text-muted-foreground transition-transform ${collapsed ? "-rotate-90" : ""}`}
                />
                <span className="font-mono text-[12px] font-medium">{group.module}</span>
                <span className="truncate text-[12px]">{group.doc}</span>
                <Meta className="flex-1 text-[11px]">
                  {group.rows.length} requirements
                </Meta>
                <Meta className="text-[11px]">{tallyLine(group.counts)}</Meta>
              </button>

              {!collapsed && summarise ? (
                <div className="flex items-center gap-2.5 border-b bg-verdict-missing-bg py-2 pr-4 pl-9">
                  <span className="flex flex-none items-baseline gap-1.5 text-[11.5px] text-verdict-missing">
                    <span className="font-mono font-medium">{missingRows.length}</span>
                    missing in {group.module}
                  </span>
                  <p className="min-w-0 flex-1 text-[11px] leading-[15px] text-muted-foreground">
                    {missingSplit(missingRows)}
                  </p>
                  <Button
                    variant="outline"
                    size="xs"
                    className="flex-none"
                    onClick={() => {
                      const next = new Set(expandedMissing);
                      if (next.has(group.module)) next.delete(group.module);
                      else next.add(group.module);
                      setExpandedMissing(next);
                    }}
                  >
                    <ChevronDownIcon
                      className={`size-3 transition-transform ${expanded ? "rotate-180" : ""}`}
                    />
                    {expanded ? "Collapse" : "Show all"}
                  </Button>
                </div>
              ) : null}

              {rendered.map((row) => (
                <MatrixRow
                  key={row.req_id}
                  row={row}
                  opened={row.req_id === lastOpened}
                  onOpen={open}
                />
              ))}

              {truncated > 0 ? (
                <div className="flex items-center gap-2.5 border-b py-2 pr-4 pl-9">
                  <p className="flex-1 text-[11px] text-muted-foreground">
                    {truncated} more {truncated === 1 ? "row" : "rows"} in{" "}
                    {group.module}
                  </p>
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() =>
                      setPageSizes({
                        ...pageSizes,
                        [group.module]:
                          (pageSizes[group.module] ?? PAGE_SIZE) + PAGE_SIZE,
                      })
                    }
                  >
                    Show more
                  </Button>
                </div>
              ) : null}
            </div>
          ),
        )}

        {visibleGroups.length === 0 ? (
          <p className="p-6 text-[12px] text-muted-foreground">
            No rows match that filter.
          </p>
        ) : null}
      </div>

      <div className="flex flex-none items-center border-t px-5 py-2">
        <p className="text-[11px] text-muted-foreground">
          Showing {shownRows} of {result.rows.length} rows ·{" "}
          {visibleGroups.length} {visibleGroups.length === 1 ? "group" : "groups"}
        </p>
      </div>
    </div>
  );
}

/**
 * The two-way split of a module's missing rows. Evidence presence is the
 * discriminator the judge itself used: no candidates at all versus a symbol
 * that resolved but did not implement the behaviour.
 */
function missingSplit(missingRows: readonly ReportRow[]): string {
  const bare = missingRows.filter((row) => row.evidence.length === 0).length;
  const named = missingRows.length - bare;
  return (
    `${bare} have no counterpart in the snapshot · ` +
    `${named} name a symbol that exists but carries no matching implementation`
  );
}

function MatrixRow({
  row,
  opened,
  onOpen,
}: {
  row: ReportRow;
  /** The row the reader last followed into the source pane. Marked so that
   *  coming back to a 398-row table shows where they left off, rather than
   *  making them find it again. */
  opened: boolean;
  onOpen: (
    reqId: string,
    evidence: CodeCitation | null,
    tab: "document" | "code",
  ) => void;
}) {
  return (
    <div
      className={`${GRID} cursor-pointer border-b py-2 transition-colors hover:bg-muted/60 ${
        opened ? "border-l-2 border-l-source bg-muted/40" : ""
      }`}
      onClick={() =>
        // Both tabs: the requirement's page, and — when the judge cited code —
        // the lines it cited. That pairing is what makes the matrix useful
        // rather than a list of verdicts. The row lands on the requirement;
        // the evidence cell below lands on the code.
        onOpen(row.req_id, codeCitationFor(row.evidence[0]), "document")
      }
    >
      <div className="min-w-0">
        <span className="font-mono text-[11.5px] font-medium">{row.req_id}</span>
        {row.text ? (
          <p className="mt-0.5 line-clamp-2 text-[11px] leading-[15px] text-muted-foreground">
            {row.text}
          </p>
        ) : null}
      </div>
      <div className="min-w-0">
        {row.upstream_ids.length === 0 ? (
          <span className="text-[11px] text-muted-foreground">—</span>
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
      </div>
      <div>
        <span
          className={`inline-flex rounded border px-1.5 py-px text-[10.5px] font-medium ${VERDICT_CLASS[row.status]}`}
        >
          {row.status}
        </span>
      </div>
      <div className="text-right font-mono text-[11.5px] tabular-nums">
        {row.confidence.toFixed(2)}
      </div>
      <div className="min-w-0">
        {row.evidence.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {/* Every cited span is its own control. Showing only the first and
                a "+2" left the rest unreachable — the judge cited them, so the
                reader should be able to open them. */}
            {row.evidence.map((item) => (
              <button
                key={`${item.file}:${item.lines[0]}-${item.lines[1]}`}
                type="button"
                onClick={(event) => {
                  // Without this the row handler fires too and lands on the
                  // document tab — the opposite of what was clicked.
                  event.stopPropagation();
                  onOpen(row.req_id, codeCitationFor(item), "code");
                }}
                className="rounded border border-source-border bg-source-bg px-1.5 py-px font-mono text-[10.5px] text-source transition-colors hover:brightness-110"
              >
                {item.file.split("/").pop()}:{item.lines[0]}–{item.lines[1]}
              </button>
            ))}
          </div>
        ) : null}
        {row.rationale ? (
          // Visible, not tooltip-only: the rationale is the judge's actual
          // finding, and hover was the one place nobody read it.
          <p
            className={`line-clamp-2 text-[11px] leading-[15px] text-muted-foreground ${row.evidence.length > 0 ? "mt-1" : ""}`}
          >
            {row.rationale}
          </p>
        ) : row.evidence.length === 0 ? (
          <span className="text-[11px] text-muted-foreground">—</span>
        ) : null}
      </div>
      <div className="flex justify-end pt-0.5 text-muted-foreground">
        <ChevronRightIcon className="size-3.5" />
      </div>
    </div>
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
