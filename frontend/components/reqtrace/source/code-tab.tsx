"use client";

import { useEffect, useRef } from "react";
import { CopyIcon, GitCommitHorizontalIcon, ScanLineIcon } from "lucide-react";

import type { HighlightedFile } from "@/lib/code-highlight";
import type { CodeCitation } from "@/lib/events";
import { Button } from "@/components/ui/button";
import { CodeTokens } from "@/components/reqtrace/code-tokens";
import { Cap, Meta } from "@/components/reqtrace/text";

/** Canvas artboard 3: 11.8 px mono on a 17 px line, 52 px right-aligned gutter. */
const LINE_HEIGHT = 17;
const PAD_TOP = 10;

export function CodeTab({
  file,
  citation,
}: {
  file: HighlightedFile;
  citation: CodeCitation | null;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastLine = file.first_line + file.lines.length - 1;

  const span = citation?.line_span ?? null;
  // The committed slice is a window on a 1 927-line file. A span outside it is
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
  }, [bandStart, file.first_line]);

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
        <Button variant="ghost" size="icon-xs" aria-label="Copy path" disabled>
          <CopyIcon />
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
              evidence span
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
                annotation, never a verdict — verdict lives in the chat answer. */}
            {line.annotation ? (
              <span
                aria-hidden
                className={
                  line.annotation === "negative"
                    ? "absolute top-[3px] bottom-[3px] left-0 w-0.5 rounded-sm bg-verdict-partial"
                    : "absolute top-[3px] bottom-[3px] left-0 w-0.5 rounded-sm bg-source"
                }
              />
            ) : null}
            <span className="w-[52px] flex-none pr-3 text-right text-code-gutter select-none">
              {line.number}
            </span>
            <span className="flex-1 whitespace-pre">
              <CodeTokens tokens={line.tokens} />
            </span>
          </div>
        ))}

        <div className="h-6" />
      </div>

      <div className="flex-none border-t bg-muted px-3 py-1.5">
        <Meta>
          Committed slice, lines {file.first_line}&ndash;{lastLine} of{" "}
          {file.total_lines.toLocaleString("en-US")}. Live slices arrive with{" "}
          <span className="text-foreground">
            GET /code/{"{path}"}?lines=a-b
          </span>{" "}
          in WP4; the pane can only ever open paths inside the pinned snapshot.
        </Meta>
      </div>
    </div>
  );
}
