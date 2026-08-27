"use client";

import { CircleHelpIcon, CodeIcon, FileTextIcon } from "lucide-react";

import type {
  CodeCitation,
  RequirementCitation,
  UpstreamCitation,
} from "@/lib/events";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Cap } from "@/components/reqtrace/text";

/**
 * Citation chips.
 *
 * They come from `citation` events and nothing else. No part of the answer body
 * is a link, so there is no path by which a citation could be parsed out of
 * prose — which is what spec §7 requires and what makes thread export keep
 * citations intact.
 *
 * Requirement ids are printed exactly as the corpus spells them. Casing varies
 * per document (`SWS_Can_00011`, `SWS_CANIF_00001`, `SWS_CanTp_00002`) and is
 * never normalised, here or anywhere.
 */
export function CitationChips({
  sources,
  upstream,
  onOpen,
}: {
  sources: readonly (RequirementCitation | CodeCitation)[];
  upstream: readonly UpstreamCitation[];
  onOpen: (citation: RequirementCitation | CodeCitation) => void;
}) {
  if (sources.length === 0 && upstream.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5">
      {sources.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Cap className="mr-px">Sources</Cap>
          {sources.map((citation) => (
            <SourceChip
              key={sourceKey(citation)}
              citation={citation}
              onOpen={onOpen}
            />
          ))}
        </div>
      ) : null}

      {upstream.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Cap className="mr-px">Upstream</Cap>
          {upstream.map((citation) => (
            <UpstreamChip
              key={`${citation.req_id}:${citation.cited_by}`}
              citation={citation}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SourceChip({
  citation,
  onOpen,
}: {
  citation: RequirementCitation | CodeCitation;
  onOpen: (citation: RequirementCitation | CodeCitation) => void;
}) {
  const isCode = citation.kind === "code";
  const Icon = isCode ? CodeIcon : FileTextIcon;
  const label = isCode
    ? `${basename(citation.repo_path)} · L${citation.line_span[0]}–${citation.line_span[1]}`
    : `${citation.req_id} · p. ${citation.page}`;

  return (
    <button
      type="button"
      onClick={() => onOpen(citation)}
      className="flex h-[22px] max-w-full items-center gap-1.5 rounded-md border border-source-border bg-source-bg px-[7px] font-mono text-[11.5px] leading-none font-medium text-source transition-colors hover:bg-source-bg/70"
    >
      <Icon className="size-3 flex-none" />
      <span className="truncate">{label}</span>
    </button>
  );
}

/**
 * SRS/RS chips: dashed, unfilled, help-iconed, and NOT a button. SRS documents
 * are not in the index, so there is genuinely no page to open and the chip must
 * not look like it leads anywhere (spec §11).
 */
export function UpstreamChip({ citation }: { citation: UpstreamCitation }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <span
            tabIndex={0}
            className="flex h-[22px] max-w-full cursor-help items-center gap-1.5 rounded-md border border-dashed border-input bg-transparent px-[7px] font-mono text-[11.5px] leading-none text-muted-foreground"
          >
            <CircleHelpIcon className="size-3 flex-none" />
            <span className="truncate">{citation.req_id}</span>
          </span>
        }
      />
      <TooltipContent side="top" className="max-w-[280px] flex-col items-start gap-1 px-2.5 py-2 text-left">
        <span className="text-[11.5px] leading-4 font-medium">
          Upstream requirement — not ingested
        </span>
        <span className="text-[10.5px] leading-[14.5px] opacity-80">
          Cited by {citation.cited_by}. {citation.doc} is not part of the index,
          so there is no page to open.
        </span>
      </TooltipContent>
    </Tooltip>
  );
}

function sourceKey(citation: RequirementCitation | CodeCitation): string {
  return citation.kind === "code"
    ? `${citation.repo_path}:${citation.line_span[0]}`
    : `${citation.doc}:${citation.req_id}`;
}

function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}
