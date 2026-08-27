"use client";

/**
 * `GET /api/py/code/{path}?lines=a-b` — the file behind a code citation
 * (story S5.3.2).
 *
 * Until this existed the Code tab rendered one committed fixture whatever the
 * citation said, so an evidence chip pointing at `CanTp.c` opened `CanIf.c`.
 * It now fetches the cited file from the indexed snapshot.
 *
 * **A window, not the file.** `CanIf.c` is 1,927 lines and a citation is
 * usually a 40-line function. The request asks for the span plus
 * `CONTEXT_LINES` either side, which is enough to read what surrounds the
 * evidence without shipping the whole file for every chip. `first_line` comes
 * back with the slice, so the gutter shows real line numbers rather than
 * counting from one.
 *
 * **Highlighting is the browser's here.** The Code tab's original file is
 * highlighted on the server, where the page already knew what it was rendering
 * (`lib/code-highlight.ts`). A citation is only known at click time, so this
 * reuses the fine-grained client highlighter that the chat's code fences
 * already load — same three grammars, same theme, no second Shiki bundle.
 */

import { useEffect, useState } from "react";

import type { HighlightedFile } from "@/lib/code-highlight";
import { highlightSnippet } from "@/lib/code-highlight-client";
import type { CodeCitation } from "@/lib/events";

/** Lines fetched either side of the cited span. */
const CONTEXT_LINES = 40;

/** Mirrors `CodeSliceResponse` in `api/documents.py`. */
interface CodeSlice {
  repo_path: string;
  git_sha: string;
  language: string;
  first_line: number;
  last_line: number;
  total_lines: number;
  text: string;
}

const POSITIVE_ANNOTATION = /@req\s+\S+/;
const NEGATIVE_ANNOTATION = /!req\s+\S+/;

function annotationOf(line: string): HighlightedFile["lines"][number]["annotation"] {
  // A `!req` on the same line wins: it is the stronger claim.
  if (NEGATIVE_ANNOTATION.test(line)) return "negative";
  if (POSITIVE_ANNOTATION.test(line)) return "positive";
  return null;
}

export type CodeFileState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; file: HighlightedFile }
  | { phase: "failed"; message: string };

export function useCodeFile(citation: CodeCitation | null): CodeFileState {
  // Keyed by the request that produced it, so a new citation shows as loading
  // without an effect writing state during render.
  const [result, setResult] = useState<{
    token: string;
    state: CodeFileState;
  } | null>(null);

  const token = citation
    ? `${citation.repo_path}:${citation.line_span[0]}-${citation.line_span[1]}`
    : "";

  useEffect(() => {
    if (!citation) return;
    let cancelled = false;

    const [start, end] = citation.line_span;
    const from = Math.max(1, start - CONTEXT_LINES);
    const to = end + CONTEXT_LINES;
    const path = citation.repo_path.split("/").map(encodeURIComponent).join("/");

    fetch(`/api/py/code/${path}?lines=${from}-${to}`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<CodeSlice>;
      })
      .then(async (slice) => {
        const rawLines = slice.text.split("\n");
        const tokens = await highlightSnippet(slice.text, "c");
        if (cancelled) return;
        setResult({
          token: `${citation.repo_path}:${start}-${end}`,
          state: {
            phase: "ready",
            file: {
              path: slice.repo_path,
              git_sha: slice.git_sha,
              first_line: slice.first_line,
              total_lines: slice.total_lines,
              lines: tokens.map((lineTokens, index) => ({
                number: slice.first_line + index,
                annotation: annotationOf(rawLines[index] ?? ""),
                tokens: lineTokens,
              })),
            },
          },
        });
      })
      .catch(() => {
        if (cancelled) return;
        setResult({
          token: `${citation.repo_path}:${start}-${end}`,
          state: {
            phase: "failed",
            message: `${citation.repo_path} could not be read from the indexed snapshot.`,
          },
        });
      });

    return () => {
      cancelled = true;
    };
  }, [citation, token]);

  if (!citation) return { phase: "idle" };
  if (result?.token === token) return result.state;
  return { phase: "loading" };
}
