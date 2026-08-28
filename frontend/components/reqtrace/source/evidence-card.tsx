"use client";

import { useEffect, useState } from "react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  FileTextIcon,
} from "lucide-react";

import { VERDICTS, type RequirementCitation, type Verdict } from "@/lib/events";
import {
  fetchImplementation,
  type Implementation,
  type ImplementationLink,
} from "@/lib/requirements";
import { Button } from "@/components/ui/button";
import { Cap, Meta } from "@/components/reqtrace/text";

/**
 * The Evidence card (canvas artboard 3, right column — decided to live in the
 * Code tab): what the judge concluded about the requirement this code was
 * opened for, and how the evidence was found.
 *
 * **Cached verdicts only.** The lookup behind `useImplementation` never judges
 * — a verdict appears here only if a report or a `check_implementation` call
 * already paid for it. No verdict, no card: rendering "not judged yet" would
 * invite a button that spends money on every citation click.
 */
export function useImplementation(reqId: string | null): Implementation | null {
  // Keyed by the id that produced it, so switching requirements never shows
  // the previous one's verdict — and no state is written during render.
  const [result, setResult] = useState<{
    token: string;
    implementation: Implementation | null;
  } | null>(null);

  useEffect(() => {
    if (!reqId) return;
    let cancelled = false;
    // No-op in canned mode (`fetchImplementation` checks `chatSourceKind`),
    // so the offline demo stays offline.
    void fetchImplementation(reqId).then((implementation) => {
      if (!cancelled) setResult({ token: reqId, implementation });
    });
    return () => {
      cancelled = true;
    };
  }, [reqId]);

  return result?.token === reqId ? result.implementation : null;
}

/** Same classes as the report matrix — a verdict must look the same everywhere. */
const VERDICT_CLASS: Record<Verdict, string> = {
  implemented:
    "border-verdict-implemented-border bg-verdict-implemented-bg text-verdict-implemented",
  partial: "border-verdict-partial-border bg-verdict-partial-bg text-verdict-partial",
  missing: "border-verdict-missing-border bg-verdict-missing-bg text-verdict-missing",
  unverifiable:
    "border-verdict-unverifiable-border bg-verdict-unverifiable-bg text-verdict-unverifiable",
};

export function EvidenceCard({
  requirement,
  implementation,
  stepper,
  onOpenRequirement,
}: {
  requirement: RequirementCitation;
  implementation: Implementation;
  /** Present only when the verdict cited several spans in the open file. */
  stepper: { index: number; count: number; onStep: (delta: number) => void } | null;
  onOpenRequirement?: (citation: RequirementCitation) => void;
}) {
  const [open, setOpen] = useState(true);
  const verdict = implementation.verdict;
  if (!verdict) return null;

  const status = (VERDICTS as readonly string[]).includes(verdict.status)
    ? (verdict.status as Verdict)
    : null;
  const annotations = implementation.links.filter(
    (link) => link.found_by === "annotation",
  );
  const anchors = implementation.links.filter((link) => link.found_by === "symbol");

  return (
    <div className="flex-none border-b px-3 py-2">
      <div className="flex h-6 items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        >
          <Cap>Evidence</Cap>
          {open ? (
            <ChevronUpIcon className="size-3 text-muted-foreground" />
          ) : (
            <ChevronDownIcon className="size-3 text-muted-foreground" />
          )}
        </button>
        {stepper ? (
          <span className="flex flex-none items-center gap-0.5">
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Previous evidence span"
              disabled={stepper.index === 0}
              onClick={() => stepper.onStep(-1)}
            >
              <ChevronLeftIcon />
            </Button>
            <Meta className="tabular-nums">
              {stepper.index + 1} / {stepper.count}
            </Meta>
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Next evidence span"
              disabled={stepper.index === stepper.count - 1}
              onClick={() => stepper.onStep(1)}
            >
              <ChevronRightIcon />
            </Button>
          </span>
        ) : null}
      </div>

      {open ? (
        <div className="pt-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`flex h-6 flex-none items-center gap-1.5 rounded-md border px-2 text-[11.5px] leading-none font-medium ${
                status ? VERDICT_CLASS[status] : "bg-muted text-muted-foreground"
              }`}
            >
              {verdict.status}
              <span className="font-mono text-[11px] tabular-nums opacity-75">
                {verdict.confidence.toFixed(2)}
              </span>
            </span>
            <span className="flex h-6 flex-none items-center gap-1 rounded-md border border-source-border bg-source-bg px-2 font-mono text-[11px] leading-none font-medium text-source">
              <FileTextIcon className="size-3" />
              {requirement.req_id}
            </span>
          </div>

          {verdict.rationale ? (
            <>
              <Cap className="mt-2 block">Judge rationale</Cap>
              <p className="mt-0.5 text-[11.5px] leading-4 text-muted-foreground">
                {verdict.rationale}
              </p>
            </>
          ) : null}

          <Cap className="mt-2 block">How it was found</Cap>
          {annotations.length > 0 ? (
            <TierRow tier="T1" label="annotation">
              {annotationSummary(annotations)}
            </TierRow>
          ) : null}
          {anchors.length > 0 ? (
            <TierRow tier="T2" label="anchors">
              named_symbols: {anchors[0].symbol}
              {anchors.length > 1 ? ` +${anchors.length - 1} more` : ""}
            </TierRow>
          ) : null}
          {verdict.model_id ? (
            <TierRow tier="T3" label="judge">
              {verdict.model_id}
            </TierRow>
          ) : null}

          <div className="mt-2">
            <Button
              variant="outline"
              size="xs"
              onClick={() => onOpenRequirement?.(requirement)}
            >
              <FileTextIcon />
              Open requirement
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function TierRow({
  tier,
  label,
  children,
}: {
  tier: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-2 py-1">
      <span className="mt-px flex h-4 w-5 flex-none items-center justify-center rounded-[4px] bg-muted font-mono text-[10px] text-muted-foreground">
        {tier}
      </span>
      <span className="w-[82px] flex-none text-[11.5px] leading-4">{label}</span>
      <Meta className="min-w-0 flex-1 leading-4 break-words">{children}</Meta>
    </div>
  );
}

/** `@req · CanIf.c:607`, from the strongest annotation link — the id it names
 *  is already on the card's requirement chip. */
function annotationSummary(links: ImplementationLink[]): string {
  const link = links[0];
  const marker = link.claim === "claimed_not_implemented" ? "!req" : "@req";
  const file = link.repo_path.split("/").at(-1) ?? link.repo_path;
  const line = link.annotation_lines[0];
  const at = line != null ? ` · ${file}:${line}` : ` · ${file}`;
  const more = links.length > 1 ? ` +${links.length - 1} more` : "";
  return `${marker}${at}${more}`;
}
