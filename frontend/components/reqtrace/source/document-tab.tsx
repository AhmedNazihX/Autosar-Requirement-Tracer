"use client";

import {
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  FileTextIcon,
  InfoIcon,
  MinusIcon,
  ZoomInIcon,
} from "lucide-react";

import type { RequirementCitation } from "@/lib/events";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Cap, Meta } from "@/components/reqtrace/text";

/**
 * The Document tab, in its designed awaiting-backend state.
 *
 * pdf.js is pre-approved (spec §7) but deliberately not wired here:
 * `GET /documents/{doc}/view` is a WP4 story and the PDFs are gitignored, so
 * there is no page image and no stored bbox to draw a highlight from. Faking
 * either would put a fabricated rectangle on screen — the exact failure the
 * canvas's `source-states` note exists to prevent.
 *
 * What it does show is everything the `citation` event actually carries: the
 * document, the page, the section, and the verbatim extracted requirement text
 * with its AUTOSAR `⌈ ⌋` body brackets. That is real, and it is useful now.
 *
 * The treatment is the canvas's pane-B treatment: a muted info line with an info
 * icon, never the destructive tint. A missing page image is an absent feature,
 * not an error.
 */
export function DocumentTab({
  citation,
}: {
  citation: RequirementCitation | null;
}) {
  if (!citation) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 bg-muted px-10 text-center">
        <FileTextIcon className="size-5 text-muted-foreground" />
        <p className="text-[12.5px] leading-[18px] text-muted-foreground">
          No requirement open. Click a source chip under an answer to bring its
          page here.
        </p>
      </div>
    );
  }

  const pageLabel = citation.page_span
    ? `${citation.page_span[0]}–${citation.page_span[1]} / ${citation.page_count}`
    : `${citation.page} / ${citation.page_count}`;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-[34px] flex-none items-center gap-2 border-b pr-2 pl-3">
        {/* The manifest key, not a filename — so no `.pdf` is appended. */}
        <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
          {citation.doc}
        </span>
        <div className="flex flex-none items-center gap-px">
          <Button variant="ghost" size="icon-xs" aria-label="Previous page" disabled>
            <ChevronLeftIcon />
          </Button>
          <span className="font-mono text-[11px]">{pageLabel}</span>
          <Button variant="ghost" size="icon-xs" aria-label="Next page" disabled>
            <ChevronRightIcon />
          </Button>
        </div>
        <span aria-hidden className="h-4 w-px flex-none bg-border" />
        <div className="flex flex-none items-center gap-px">
          <Button variant="ghost" size="icon-xs" aria-label="Zoom out" disabled>
            <MinusIcon />
          </Button>
          <span className="font-mono text-[11px]">77%</span>
          <Button variant="ghost" size="icon-xs" aria-label="Zoom in" disabled>
            <ZoomInIcon />
          </Button>
        </div>
        <Button variant="ghost" size="icon-xs" aria-label="Download" disabled>
          <DownloadIcon />
        </Button>
      </div>

      <div className="flex flex-none items-start gap-2 border-b bg-muted px-3 py-2">
        <InfoIcon className="mt-px size-3.5 flex-none text-muted-foreground" />
        <p className="flex-1 text-[11.5px] leading-4 text-muted-foreground">
          <span className="font-medium text-foreground">
            The page image is not rendered yet.
          </span>{" "}
          pdf.js renders it from{" "}
          <span className="font-mono">
            GET /documents/{citation.doc}/view?highlight={citation.req_id}
          </span>
          , which lands in WP4. The requirement text below is what ingestion
          extracted for this citation.
          {citation.bbox === null ? (
            <>
              {" "}
              This citation also carries{" "}
              <span className="font-mono">bbox: null</span>
              {citation.page_span
                ? ` — the requirement runs across pages ${citation.page_span[0]}–${citation.page_span[1]}, so there is no single rectangle to draw.`
                : ", so no highlight rectangle will be drawn until ingestion has run in this workspace."}
            </>
          ) : null}
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-auto bg-muted px-6 pt-4 pb-6">
        <div className="mx-auto flex max-w-[560px] flex-col gap-3 rounded-[3px] border bg-background p-5 shadow-sm">
          <div className="flex items-baseline justify-between gap-3 border-b pb-2">
            <Meta className="truncate">{citation.doc_title}</Meta>
            <Meta className="flex-none">AUTOSAR CP R23-11</Meta>
          </div>

          {citation.section ? (
            <p className="text-[13px] leading-[17px] font-semibold">
              {citation.section}
            </p>
          ) : null}

          <div className="rounded-r-lg border-l-2 border-source bg-source-bg/60 px-3 py-2.5">
            <p className="text-[12.5px] leading-[18px]">
              <span className="font-mono font-bold">[{citation.req_id}]</span>{" "}
              <span className="font-serif text-[15px]">&#8968;</span>{" "}
              {citation.quote ?? (
                <span className="text-muted-foreground">
                  No extracted text on this citation.
                </span>
              )}{" "}
              <span className="font-serif text-[15px]">&#8971;</span>
            </p>
          </div>

          <div className="flex items-baseline justify-between gap-3 border-t pt-2">
            <Meta>
              {citation.page} of {citation.page_count}
            </Meta>
            <Meta className="truncate">Document key: {citation.doc}</Meta>
          </div>

          <div className="flex flex-col gap-2 pt-1">
            <Cap>Page image</Cap>
            <Skeleton className="h-40 w-full rounded-[3px]" />
            <Meta>
              Reserved for the rendered page and its highlight overlay.
            </Meta>
          </div>
        </div>
      </div>
    </div>
  );
}
