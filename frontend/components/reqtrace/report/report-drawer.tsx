"use client";

import { useRef, useState } from "react";
import { RotateCcwIcon, TriangleAlertIcon, XIcon } from "lucide-react";

import type { CodeCitation, Verdict } from "@/lib/events";
import { useReport } from "@/hooks/use-report";
import type { DocumentStatus } from "@/hooks/use-setup-status";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { LaunchForm } from "./launch-form";
import { Progress } from "./progress";
import { Matrix } from "./matrix";

/**
 * The report drawer (feature F5.5) — the Sheet shell and the phase switch.
 *
 * Three states in one surface, because they are one task: choose a scope and
 * see what it will cost, watch it run, read the matrix. The drawer never
 * unmounts between them, so a run keeps going while the user reads the chat
 * behind it and closing it does not cancel anything. The panels themselves
 * live in `launch-form.tsx`, `progress.tsx` and `matrix.tsx`; this file only
 * decides which one is on screen.
 *
 * **The cost estimate is shown before the button, not after.** Judging is the
 * only expensive thing this application does, and the number comes from
 * OpenRouter's own published prices (`core/pricing.py`). When it cannot be
 * known the drawer says so and shows the count instead — an invented figure
 * beside a Launch button is worse than no figure.
 */
/**
 * Where the reader had got to in the matrix.
 *
 * Held by the shell, not by the drawer, for the same reason the run is: Base
 * UI unmounts a closed popup, and the drawer now closes every time you follow
 * an evidence span into the code. Without this, coming back put you at the top
 * of 398 rows with your filters cleared — so following a piece of evidence
 * cost you your place, every time. The search, the collapsed groups, the
 * expanded missing sets and the per-module page depths are part of "your
 * place" for the same reason the scroll offset is: restoring the offset only
 * helps if the same rows render under it.
 *
 * `scrollTopRef` is a ref rather than state on purpose: it changes on every
 * wheel event and nothing needs to re-render when it does.
 */
export interface ReportView {
  filters: Set<Verdict>;
  setFilters: (next: Set<Verdict>) => void;
  query: string;
  setQuery: (next: string) => void;
  /** Modules whose whole group is folded to its header row. */
  collapsedGroups: Set<string>;
  setCollapsedGroups: (next: Set<string>) => void;
  /** Modules whose missing rows are shown despite the default collapse. */
  expandedMissing: Set<string>;
  setExpandedMissing: (next: Set<string>) => void;
  /** Per-module "Show more" depth, in rows. */
  pageSizes: Record<string, number>;
  setPageSizes: (next: Record<string, number>) => void;
  scrollTopRef: React.RefObject<number>;
  lastOpened: string | null;
  setLastOpened: (reqId: string) => void;
}

export function useReportView(): ReportView {
  const [filters, setFilters] = useState<Set<Verdict>>(new Set());
  const [query, setQuery] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [expandedMissing, setExpandedMissing] = useState<Set<string>>(new Set());
  const [pageSizes, setPageSizes] = useState<Record<string, number>>({});
  const [lastOpened, setLastOpened] = useState<string | null>(null);
  const scrollTopRef = useRef(0);
  return {
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
  };
}

/**
 * Shown only when there is no `SetupStatus` to derive the real list from —
 * canned mode has no backend to ask. In live mode the shell passes the
 * documents from `GET /setup/status`, the same manifest-derived source the
 * backend resolves a scope against, so a document added to the manifest
 * appears here without a frontend change. This list going stale was a real
 * bug: CanNm/Com/PduR were ingested and the drawer still offered four.
 * Requirement counts are unknown without a backend, and 0 renders as none.
 */
const FALLBACK_DOCUMENTS: readonly DocumentStatus[] = [
  { key: "can", title: "AUTOSAR SWS CAN Driver", module: "Can", page_count: 0, requirements: 0 },
  { key: "canif", title: "AUTOSAR SWS CAN Interface", module: "CanIf", page_count: 0, requirements: 0 },
  { key: "cantp", title: "AUTOSAR SWS CAN Transport Layer", module: "CanTp", page_count: 0, requirements: 0 },
  { key: "cansm", title: "AUTOSAR SWS CAN State Manager", module: "CanSM", page_count: 0, requirements: 0 },
];

export function ReportDrawer({
  open,
  onOpenChange,
  onOpenRow,
  defaultModule,
  documents = FALLBACK_DOCUMENTS,
  gitSha = null,
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
  /** Manifest documents from `setup.documents`; `FALLBACK_DOCUMENTS` when
   *  there is no backend to ask. */
  documents?: readonly DocumentStatus[];
  /** The pinned snapshot SHA from setup, for the launch form's cache caption. */
  gitSha?: string | null;
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
        className="flex w-full flex-col gap-0 p-0 data-[side=right]:sm:max-w-[1200px]"
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
            documents={documents}
            gitSha={gitSha}
            onLaunch={(scope) => void report.launch(scope)}
          />
        ) : null}

        {report.state.phase === "running" ? (
          <Progress
            launch={report.state.launch}
            progress={report.state.progress}
            startedAt={report.state.startedAt}
            onCancel={report.cancel}
            onClose={() => onOpenChange(false)}
          />
        ) : null}

        {report.state.phase === "finished" ? (
          <Matrix
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
            <div className="flex items-start gap-2.5 rounded-lg border border-destructive-border bg-destructive-bg px-3.5 py-3">
              <TriangleAlertIcon className="mt-0.5 size-3.5 flex-none text-destructive" />
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
