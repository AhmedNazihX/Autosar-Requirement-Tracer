/**
 * Server-side syntax highlighting for the Code tab.
 *
 * Shiki runs here and only here. The page is a Server Component, so the
 * highlighter, its C grammar and the oniguruma WASM never reach the browser
 * bundle; what crosses the boundary is a plain serialisable token array.
 *
 * The output shape is deliberately structural rather than an HTML string: the
 * Code tab has to draw its own line-number gutter, per-line annotation bars and
 * the evidence band, and it cannot do that against `innerHTML`.
 */

import { codeToTokens } from "shiki";

import { reqtraceCodeTheme } from "./code-theme";
import {
  CANIF_FIXTURE_FIRST_LINE,
  CANIF_FIXTURE_PATH,
  CANIF_FIXTURE_SHA,
  CANIF_FIXTURE_SOURCE,
  CANIF_FIXTURE_TOTAL_LINES,
} from "./fixtures/canif-c";

export interface HighlightedToken {
  content: string;
  /** A `var(--code-*)` reference, straight from the theme. */
  color?: string;
  italic?: boolean;
  bold?: boolean;
}

export interface HighlightedLine {
  /** Absolute 1-based line number in the real file. */
  number: number;
  tokens: HighlightedToken[];
  /**
   * Requirement-annotation polarity found on this line, if any. Drives the
   * 2 px gutter bar — blue for `@req`, ochre for `!req`. This is a lexical
   * scan of the committed slice, not a verdict: verdict lives in the side
   * panel, never in the file (canvas artboard 3).
   */
  annotation: "positive" | "negative" | null;
}

export interface HighlightedFile {
  path: string;
  git_sha: string;
  first_line: number;
  total_lines: number;
  lines: HighlightedLine[];
}

/** `@req 4.0.3/CANIF005` and `!req CANIF058` both appear in the real snapshot. */
const POSITIVE_ANNOTATION = /@req\s+\S+/;
const NEGATIVE_ANNOTATION = /!req\s+\S+/;

function annotationOf(line: string): HighlightedLine["annotation"] {
  // A `!req` on the same line wins: it is the stronger claim.
  if (NEGATIVE_ANNOTATION.test(line)) return "negative";
  if (POSITIVE_ANNOTATION.test(line)) return "positive";
  return null;
}

let cached: HighlightedFile | null = null;

export async function highlightCodeFixture(): Promise<HighlightedFile> {
  if (cached) return cached;

  const { tokens } = await codeToTokens(CANIF_FIXTURE_SOURCE, {
    lang: "c",
    theme: reqtraceCodeTheme,
  });

  const rawLines = CANIF_FIXTURE_SOURCE.split("\n");

  cached = {
    path: CANIF_FIXTURE_PATH,
    git_sha: CANIF_FIXTURE_SHA,
    first_line: CANIF_FIXTURE_FIRST_LINE,
    total_lines: CANIF_FIXTURE_TOTAL_LINES,
    lines: tokens.map((lineTokens, index) => ({
      number: CANIF_FIXTURE_FIRST_LINE + index,
      annotation: annotationOf(rawLines[index] ?? ""),
      tokens: lineTokens.map((token) => {
        // Shiki's FontStyle is a bitmask (Italic 1, Bold 2, Underline 4,
        // Strikethrough 8), so combinations must be masked, not compared.
        // `NotSet` is -1 and every mask matches it, so negatives are floored to
        // 0 rather than read as "italic and bold and underlined".
        const style =
          token.fontStyle && token.fontStyle > 0 ? token.fontStyle : 0;
        return {
          content: token.content,
          color: token.color,
          italic: (style & 1) !== 0,
          bold: (style & 2) !== 0,
        };
      }),
    })),
  };

  return cached;
}
