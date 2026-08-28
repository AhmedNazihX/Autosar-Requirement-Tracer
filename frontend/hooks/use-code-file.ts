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

import { annotationOf } from "@/lib/code-facts";
import { chatSourceKind } from "@/lib/chat-sources";

import type { HighlightedFile } from "@/lib/code-highlight";
import { highlightSnippet, resolveLanguage } from "@/lib/code-highlight-client";
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

export type CodeFileState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; file: HighlightedFile }
  | { phase: "failed"; message: string };

export function useCodeFile(
  citation: CodeCitation | null,
  /** The span the window should centre on when it is not the citation's own —
   *  the Evidence card's stepper moves it between a verdict's cited spans. */
  focus?: [number, number] | null,
): CodeFileState {
  // Canned mode has no backend by definition — it is the demo that runs on a
  // fresh clone with no index and no key. Fetching there turns the source pane
  // into an error panel, which is a worse demo than the committed fixture it
  // was showing before.
  const live = chatSourceKind() === "live";
  // Keyed by the request that produced it, so a new citation shows as loading
  // without an effect writing state during render. The citation key rides
  // along so a *stepped* window (same citation, different span) can keep the
  // current slice on screen while the next one loads — real line numbers stay
  // real, only the band is briefly behind.
  const [result, setResult] = useState<{
    cite: string;
    windowKey: string;
    state: CodeFileState;
  } | null>(null);

  const span = focus ?? citation?.line_span ?? null;
  const cite = citation
    ? `${citation.repo_path}:${citation.line_span[0]}-${citation.line_span[1]}`
    : "";
  const windowKey = citation && span
    ? `${citation.repo_path}:${span[0]}-${span[1]}`
    : "";

  useEffect(() => {
    if (!citation || !span || !live) return;
    let cancelled = false;

    const [start, end] = span;
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
        // The backend states the language; "c" only covers its absence.
        const language = resolveLanguage(slice.language) ?? "c";
        const tokens = await highlightSnippet(slice.text, language);
        if (cancelled) return;
        setResult({
          cite,
          windowKey: `${citation.repo_path}:${start}-${end}`,
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
          cite,
          windowKey: `${citation.repo_path}:${start}-${end}`,
          state: {
            phase: "failed",
            message: `${citation.repo_path} could not be read from the indexed snapshot.`,
          },
        });
      });

    return () => {
      cancelled = true;
    };
  }, [citation, span, cite, windowKey, live]);

  if (!citation || !live) return { phase: "idle" };
  if (result?.windowKey === windowKey) return result.state;
  // A stepped window on the citation already shown: the slice on screen is
  // still the right file at real line numbers, so it stays up rather than
  // flashing a skeleton between spans.
  if (result?.cite === cite && result.state.phase === "ready") return result.state;
  return { phase: "loading" };
}
