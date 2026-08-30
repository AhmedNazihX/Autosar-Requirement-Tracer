"use client";

import { useEffect, useRef, useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  CopyIcon,
  GitCommitHorizontalIcon,
  ScanLineIcon,
} from "lucide-react";

import type { HighlightedFile } from "@/lib/code-highlight";
import { useCodeFile } from "@/hooks/use-code-file";
import type { CodeCitation, RequirementCitation } from "@/lib/events";
import {
  citationForLink,
  type Implementation,
  type LinkedRequirement,
} from "@/lib/requirements";
import { useCodeRequirements } from "@/hooks/use-code-requirements";
import { Button } from "@/components/ui/button";
import { CodeTokens } from "@/components/reqtrace/code-tokens";
import { Skeleton } from "@/components/ui/skeleton";
import { Cap, Meta } from "@/components/reqtrace/text";

import { EvidenceCard, useImplementation } from "./evidence-card";

/** Canvas artboard 3: 11.8 px mono on a 17 px line, 52 px right-aligned gutter. */
const LINE_HEIGHT = 17;
const PAD_TOP = 10;

/**
 * The Code tab.
 *
 * **With no citation it shows nothing, and that is the point.** It used to
 * render the committed fixture as a "resting state" — a real slice of
 * `CanIf.c`, with a real SHA and real line numbers, sitting beside answers
 * that had never pointed at any code. In a tool whose entire claim is that
 * every answer is source-linked, presenting arbitrary source as though it were
 * relevant is the worst thing this pane can do: it is indistinguishable from
 * evidence. The Document tab always had the right behaviour here; this one was
 * carried over from before the backend existed, when the fixture was all there
 * was.
 *
 * `fallback` is now used for exactly one case: a code citation in **canned**
 * mode, where there is no backend to fetch from and the fixture *is* the cited
 * file (`CANIF_FIXTURE_PATH` is the only path the canned conversation cites).
 * In live mode story S5.3.2 fetches the cited file — before it did, an evidence
 * chip pointing at `CanTp.c` opened `CanIf.c` and looked right.
 */
export function CodeTab({
  file: fallback,
  citation,
  requirement,
  onOpenRequirement,
}: {
  file: HighlightedFile;
  citation: CodeCitation | null;
  /** The requirement this code was opened for — the Document tab's half of the
   *  citation pair, when the pane knows it. Names the evidence pill and keys
   *  the Evidence card's verdict lookup. */
  requirement?: RequirementCitation | null;
  /** Open one of the requirements this code is tied to, in the Document tab. */
  onOpenRequirement?: (citation: RequirementCitation) => void;
}) {
  const links = useCodeRequirements(citation);
  // Cached verdict only — `fetchImplementation` never judges, and it no-ops
  // in canned mode, so this costs nothing however often the pane changes.
  const implementation = useImplementation(requirement?.req_id ?? null);

  // The spans a cached verdict cited in the open file. More than one earns the
  // Evidence card its stepper; spans in *other* files are not offered, because
  // stepping there would silently swap the file under the citation.
  const verdictSpans = (() => {
    if (!citation) return [];
    const seen = new Set<string>();
    const spans: [number, number][] = [];
    for (const item of implementation?.verdict?.evidence ?? []) {
      if (item.file !== citation.repo_path) continue;
      const key = `${item.lines[0]}-${item.lines[1]}`;
      if (seen.has(key)) continue;
      seen.add(key);
      spans.push([item.lines[0], item.lines[1]]);
    }
    return spans;
  })();

  // Keyed by what is on screen, so a new citation or requirement lands back on
  // its own span without an effect resetting state.
  const stepToken = `${requirement?.req_id ?? ""}|${
    citation
      ? `${citation.repo_path}:${citation.line_span[0]}-${citation.line_span[1]}`
      : ""
  }`;
  const [step, setStep] = useState<{ token: string; index: number } | null>(null);
  const defaultIndex = Math.max(
    0,
    verdictSpans.findIndex(
      (span) =>
        citation &&
        span[0] === citation.line_span[0] &&
        span[1] === citation.line_span[1],
    ),
  );
  const stepIndex = Math.min(
    step?.token === stepToken ? step.index : defaultIndex,
    Math.max(verdictSpans.length - 1, 0),
  );
  const activeSpan: [number, number] | null =
    verdictSpans.length > 1
      ? verdictSpans[stepIndex]
      : (citation?.line_span ?? null);
  const fetched = useCodeFile(citation, activeSpan);

  // Nothing cited this turn: say so, rather than showing code nobody pointed at.
  if (!citation) return <NoCitation />;

  // While a cited file is in flight, show that it is — never the fallback.
  // Rendering the committed fixture under the citation's line numbers is the
  // exact failure this story fixes, and it is invisible: the pane looks right.
  if (citation && fetched.phase === "loading") {
    return <Pending path={citation.repo_path} />;
  }
  if (fetched.phase === "failed") {
    return <Unavailable message={fetched.message} />;
  }

  const file = fetched.phase === "ready" ? fetched.file : fallback;
  return (
    <CodeView
      file={file}
      citation={citation}
      span={activeSpan}
      requirement={requirement ?? null}
      implementation={implementation}
      stepper={
        verdictSpans.length > 1
          ? {
              index: stepIndex,
              count: verdictSpans.length,
              onStep: (delta: number) =>
                setStep({
                  token: stepToken,
                  index: Math.min(
                    Math.max(stepIndex + delta, 0),
                    verdictSpans.length - 1,
                  ),
                }),
            }
          : null
      }
      live={fetched.phase === "ready"}
      links={links}
      onOpenRequirement={onOpenRequirement}
    />
  );
}

/**
 * The "Traces to" chip row, minimizable exactly like the Evidence card below
 * it: a Cap header that toggles, chevron state, and — collapsed — a count so
 * the header still says what it is hiding. A file tied to a dozen
 * requirements otherwise pushes the source itself below the fold.
 */
function TracesRow({
  links,
  onOpenRequirement,
}: {
  links: readonly LinkedRequirement[];
  onOpenRequirement?: (citation: RequirementCitation) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="flex-none border-b px-3 py-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex h-[22px] w-full min-w-0 items-center gap-1.5 text-left"
      >
        <Cap>Traces to</Cap>
        {open ? (
          <ChevronUpIcon className="size-3 text-muted-foreground" />
        ) : (
          <>
            <ChevronDownIcon className="size-3 text-muted-foreground" />
            <Meta>
              {links.length} requirement{links.length === 1 ? "" : "s"}
            </Meta>
          </>
        )}
      </button>
      {open ? (
        <div className="flex flex-wrap items-center gap-1.5 pt-1.5">
          {links.map((link) => {
            const denied = link.claim === "claimed_not_implemented";
            return (
              <button
                key={link.req_id}
                type="button"
                onClick={() => onOpenRequirement?.(citationForLink(link))}
                title={
                  denied
                    ? `${link.via_symbol} carries a !req for this — the developers state it is NOT implemented here`
                    : link.found_by === "annotation"
                      ? `@req in ${link.via_symbol}`
                      : `named by the requirement's own text`
                }
                className={
                  denied
                    ? "flex h-[22px] items-center gap-1 rounded-md border border-dashed border-verdict-missing-border bg-transparent px-[7px] font-mono text-[11px] leading-none text-muted-foreground line-through transition-colors hover:bg-muted"
                    : "flex h-[22px] items-center gap-1 rounded-md border border-source-border bg-source-bg px-[7px] font-mono text-[11px] leading-none text-source transition-colors hover:brightness-110"
                }
              >
                {link.req_id}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function NoCitation() {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 bg-muted px-10 text-center">
      <ScanLineIcon className="size-5 text-muted-foreground" />
      <p className="text-[12.5px] leading-[18px] text-muted-foreground">
        No code open. Click an evidence chip under an answer, or a row in a
        traceability report, to bring the cited file here.
      </p>
    </div>
  );
}

function Pending({ path }: { path: string }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-[34px] flex-none items-center gap-2 border-b pr-2 pl-3">
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
          {path}
        </span>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 p-3">
        {Array.from({ length: 14 }, (_, index) => (
          <Skeleton
            key={index}
            className="h-3"
            style={{ width: `${45 + ((index * 37) % 50)}%` }}
          />
        ))}
      </div>
    </div>
  );
}

function Unavailable({ message }: { message: string }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 bg-muted px-10 text-center">
      <ScanLineIcon className="size-5 text-muted-foreground" />
      <p className="text-[12.5px] leading-[18px] text-muted-foreground">
        {message}
      </p>
    </div>
  );
}

function CodeView({
  file,
  citation,
  span,
  requirement,
  implementation,
  stepper,
  live,
  links,
  onOpenRequirement,
}: {
  file: HighlightedFile;
  citation: CodeCitation | null;
  /** The span the band highlights — the citation's own, unless the Evidence
   *  card's stepper has moved it to another span the verdict cited. */
  span: [number, number] | null;
  requirement: RequirementCitation | null;
  implementation: Implementation | null;
  stepper: { index: number; count: number; onStep: (delta: number) => void } | null;
  /** True when `file` came from `GET /code/{path}` rather than the committed
   *  fixture. The footer says which, because "committed slice" printed under a
   *  live file is the kind of stale caption nobody re-reads. */
  live: boolean;
  links: readonly LinkedRequirement[];
  onOpenRequirement?: (citation: RequirementCitation) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastLine = file.first_line + file.lines.length - 1;
  // The rendered slice is a window on a much larger file. A span outside it is
  // reported rather than clamped into a band that would point at the wrong code.
  const spanInWindow =
    span !== null && span[0] <= lastLine && span[1] >= file.first_line;
  const bandStart = spanInWindow ? Math.max(span[0], file.first_line) : null;
  const bandEnd = spanInWindow ? Math.min(span[1], lastLine) : null;

  useEffect(() => {
    const container = scrollRef.current;
    if (!container || bandStart === null) return;
    // Put the first evidence line a little below the top edge, so the reader can
    // see what precedes it — the canvas draws the band starting inside the view.
    const offset = (bandStart - file.first_line) * LINE_HEIGHT + PAD_TOP;
    container.scrollTo({ top: Math.max(offset - 3 * LINE_HEIGHT, 0) });
  }, [bandStart, file.first_line, file.path]);

  // Transient success state for the copy-path button: the check reads as "it
  // is on your clipboard", then hands the icon back.
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (copyTimer.current) clearTimeout(copyTimer.current);
    };
  }, []);
  const copyPath = () => {
    void navigator.clipboard
      .writeText(file.path)
      .then(() => {
        setCopied(true);
        if (copyTimer.current) clearTimeout(copyTimer.current);
        copyTimer.current = setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };

  // What the pill names: the requirement the pane knows, else the cited symbol.
  const pillLabel = requirement?.req_id ?? citation?.symbol ?? null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-[34px] flex-none items-center gap-2 border-b pr-2 pl-3">
        <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
          {file.path}
        </span>
        {span ? (
          <span className="flex h-5 flex-none items-center rounded-md border border-source-border bg-source-bg px-1.5 font-mono text-[11px] leading-none font-medium text-source">
            L{span[0]}&ndash;{span[1]}
          </span>
        ) : null}
        <span className="flex h-5 flex-none items-center gap-1 rounded-md border bg-muted px-1.5 font-mono text-[10px] leading-none text-muted-foreground">
          <GitCommitHorizontalIcon className="size-3" />
          {file.git_sha.slice(0, 7)}
        </span>
        <Meta className="flex-none">
          {file.total_lines.toLocaleString("en-US")} lines
        </Meta>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Copy path"
          onClick={copyPath}
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </Button>
      </div>

      {citation ? (
        <div className="flex h-[30px] flex-none items-center gap-2 border-b bg-muted pr-2 pl-3">
          <Cap className="flex-none">Symbol</Cap>
          <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
            {citation.symbol}
          </span>
          {!spanInWindow ? (
            <Meta className="flex-none">
              outside the committed slice ({file.first_line}&ndash;{lastLine})
            </Meta>
          ) : null}
        </div>
      ) : null}

      {links.length > 0 ? (
        // Keyed by the open file so a fresh file starts expanded, the same
        // way the Evidence card resets per requirement.
        <TracesRow
          key={file.path}
          links={links}
          onOpenRequirement={onOpenRequirement}
        />
      ) : null}

      {/* Cached verdicts only, and only here: the card is where a verdict is
          allowed to appear in this tab. It is never painted onto lines — a
          verdict colour on source would read as a property of the code. */}
      {requirement && implementation?.verdict ? (
        <EvidenceCard
          key={requirement.req_id}
          requirement={requirement}
          implementation={implementation}
          stepper={stepper}
          onOpenRequirement={onOpenRequirement}
        />
      ) : null}

      <div
        ref={scrollRef}
        className="relative min-h-0 flex-1 overflow-auto bg-code-bg font-mono text-[11.8px]"
        style={{ lineHeight: `${LINE_HEIGHT}px`, paddingTop: PAD_TOP }}
      >
        {bandStart !== null && bandEnd !== null ? (
          <>
            <div
              aria-hidden
              className="absolute inset-x-0 z-0 border-l-2 border-source bg-source-bg"
              style={{
                top: (bandStart - file.first_line) * LINE_HEIGHT + PAD_TOP,
                height: (bandEnd - bandStart + 1) * LINE_HEIGHT,
              }}
            />
            <div
              className="absolute right-3.5 z-20 flex h-5 items-center gap-1.5 rounded-md bg-source px-1.5 font-mono text-[10.5px] leading-none font-medium text-background"
              style={{
                top: (bandStart - file.first_line) * LINE_HEIGHT + PAD_TOP + 3,
              }}
            >
              <ScanLineIcon className="size-3" />
              evidence span{pillLabel ? ` · ${pillLabel}` : ""}
            </div>
          </>
        ) : null}

        {file.lines.map((line) => (
          <div
            key={line.number}
            className="relative z-10 flex"
            style={{ height: LINE_HEIGHT }}
          >
            {/* 2 px gutter bar: blue for @req, ochre for !req. This marks an
                annotation, never a verdict — verdicts are shown only when
                already cached, in the Evidence card, never painted onto lines. */}
            {line.annotation ? (
              <span
                aria-hidden
                className={
                  line.annotation.polarity === "negative"
                    ? "absolute top-[3px] bottom-[3px] left-0 w-0.5 rounded-sm bg-verdict-partial"
                    : "absolute top-[3px] bottom-[3px] left-0 w-0.5 rounded-sm bg-source"
                }
              />
            ) : null}
            <span className="w-[52px] flex-none pr-3 text-right text-code-gutter select-none">
              {line.number}
            </span>
            <span className="flex-1 whitespace-pre">
              <CodeTokens tokens={line.tokens} annotation={line.annotation} />
            </span>
          </div>
        ))}

        <div className="h-6" />
      </div>

      <div className="flex-none border-t bg-muted px-3 py-1.5">
        <Meta>
          {live ? "Lines" : "Committed slice, lines"} {file.first_line}&ndash;
          {lastLine} of {file.total_lines.toLocaleString("en-US")}
          {live
            ? ", read from the indexed snapshot at this commit."
            : " — the committed fixture, which is the file this citation names. " +
              "There is no backend in this demo to read the snapshot from."}{" "}
          The pane can only ever open paths inside the pinned snapshot.
        </Meta>
      </div>
    </div>
  );
}
