"use client";

import { useState } from "react";
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
import { Cap, Meta } from "@/components/reqtrace/text";

import { PdfPage, type PdfStatus } from "./pdf-page";

/**
 * The Document tab: the real SWS page, with the citation highlighted.
 *
 * Story S5.3.1. Until it landed this pane showed the extracted requirement
 * text and said plainly that the page was not rendered — nothing was ever
 * faked, because a fabricated rectangle on a fabricated page is precisely the
 * failure the canvas's `source-states` note exists to prevent.
 *
 * Three things are true of what it shows now:
 *
 * * **The page is the real PDF**, rendered by pdf.js from
 *   `GET /documents/{doc}/file`. That endpoint had to be added — spec §6 lists
 *   only `/view`, which returns coordinates, so nothing in the design actually
 *   handed the browser a document to render.
 * * **The highlight is the stored bbox**, not a search for the text. It is what
 *   ingestion recorded when it had the page open, so it marks the requirement
 *   even where the text is split across columns or hyphenated.
 * * **The extracted text stays**, below the page. It is what every citation,
 *   answer and verdict was actually built from, and seeing it beside the page
 *   it came from is how you check the extractor rather than trust it.
 *
 * A `bbox` of `null` is normal and is stated rather than hidden: ~6% of
 * requirements cross a page break, and ingestion stores no rectangle rather
 * than a wrong one.
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

  // Keyed on the citation, so opening a different requirement resets the page
  // and zoom without an effect that writes state during render.
  return <DocumentView key={citation.req_id} citation={citation} />;
}

/** The canvas draws the page at 77%; this is that, roundable by the controls. */
const DEFAULT_SCALE = 0.8;
const MIN_SCALE = 0.5;
const MAX_SCALE = 2;

function DocumentView({ citation }: { citation: RequirementCitation }) {
  const [page, setPage] = useState(citation.page);
  const [scale, setScale] = useState(DEFAULT_SCALE);
  const [status, setStatus] = useState<PdfStatus | null>(null);

  // Until pdf.js has the document open, the citation's own `page_count` is the
  // best figure available — and it is the same number.
  const pages = status?.phase === "ready" ? status.pages : citation.page_count;
  // The highlight belongs to the citation's own page. Paging away from it must
  // not carry the rectangle onto a page the requirement is not on.
  const bbox = page === citation.page ? citation.bbox : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-[34px] flex-none items-center gap-2 border-b pr-2 pl-3">
        {/* The manifest key, not a filename — so no `.pdf` is appended. */}
        <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
          {citation.doc}
        </span>
        <div className="flex flex-none items-center gap-px">
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Previous page"
            disabled={page <= 1}
            onClick={() => setPage((value) => Math.max(1, value - 1))}
          >
            <ChevronLeftIcon />
          </Button>
          <span className="font-mono text-[11px] tabular-nums">
            {page} / {pages || "?"}
          </span>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Next page"
            disabled={pages > 0 && page >= pages}
            onClick={() => setPage((value) => Math.min(pages || value, value + 1))}
          >
            <ChevronRightIcon />
          </Button>
        </div>
        <span aria-hidden className="h-4 w-px flex-none bg-border" />
        <div className="flex flex-none items-center gap-px">
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Zoom out"
            disabled={scale <= MIN_SCALE}
            onClick={() => setScale((value) => Math.max(MIN_SCALE, value - 0.1))}
          >
            <MinusIcon />
          </Button>
          <span className="font-mono text-[11px] tabular-nums">
            {Math.round(scale * 100)}%
          </span>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Zoom in"
            disabled={scale >= MAX_SCALE}
            onClick={() => setScale((value) => Math.min(MAX_SCALE, value + 0.1))}
          >
            <ZoomInIcon />
          </Button>
        </div>
        {/* An anchor, not a Button with an onClick: middle-click and
            "open in new tab" should work on something that opens a document. */}
        <a
          href={`/api/py/documents/${encodeURIComponent(citation.doc)}/file`}
          target="_blank"
          rel="noreferrer"
          aria-label="Open the source PDF"
          title="Open the source PDF"
          className="inline-flex size-6 flex-none items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <DownloadIcon className="size-3.5" />
        </a>
      </div>

      {citation.bbox === null ? (
        <div className="flex flex-none items-start gap-2 border-b bg-muted px-3 py-2">
          <InfoIcon className="mt-px size-3.5 flex-none text-muted-foreground" />
          <p className="flex-1 text-[11.5px] leading-4 text-muted-foreground">
            <span className="font-medium text-foreground">
              No highlight rectangle for this one.
            </span>{" "}
            The requirement&rsquo;s text crosses a page break, so ingestion
            stored no bounding box rather than a wrong one. The page it starts
            on is open below.
          </p>
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-auto bg-muted px-6 pt-4 pb-6">
        <PdfPage
          doc={citation.doc}
          page={page}
          bbox={bbox}
          scale={scale}
          onStatus={setStatus}
        />

        <div className="mx-auto mt-4 flex max-w-[560px] flex-col gap-3 rounded-[3px] border bg-background p-5 shadow-sm">
          <div className="flex items-baseline justify-between gap-3 border-b pb-2">
            <Cap>What ingestion extracted</Cap>
            <Meta className="flex-none truncate">{citation.doc_title}</Meta>
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
              <span className="font-serif text-[15px]">&#8969;</span>
            </p>
          </div>

          <div className="flex items-baseline justify-between gap-3">
            <Meta>
              {citation.page} of {citation.page_count}
            </Meta>
            <Meta>Document key: {citation.doc}</Meta>
          </div>
        </div>
      </div>
    </div>
  );
}
