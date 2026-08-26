/**
 * Browser-side syntax highlighting for fenced code blocks inside assistant
 * messages.
 *
 * The Code tab highlights on the server (`lib/code-highlight.ts`), because the
 * file it renders is known before the page is sent. A code fence in a chat
 * answer is not: it arrives as `token` deltas, so it can only be highlighted in
 * the browser.
 *
 * Two rules keep that from being expensive:
 *
 *  1. Fine-grained bundle, not `shiki`'s full one. `@shikijs/core` plus the
 *     JavaScript RegExp engine and exactly three grammars — no oniguruma WASM,
 *     no 200-language index.
 *  2. Everything is behind `await import(...)`, so none of it is in the initial
 *     JS payload. A conversation with no code fence never downloads it.
 *
 * It shares `reqtraceCodeTheme` with the Code tab, which is the point: a C
 * snippet in an answer is coloured by the same eight `--code-*` tokens as the
 * same snippet in the file viewer.
 */

import { reqtraceCodeTheme } from "./code-theme";
import type { HighlightedToken } from "./code-highlight";

/**
 * The languages worth carrying. The corpus is C; answers also quote tool
 * arguments (JSON) and commands (shell). Anything else renders unhighlighted,
 * which is honest — a wrong grammar is worse than none.
 */
export type SupportedLanguage = "c" | "json" | "bash";

/** Aliases an LLM actually writes in a fence info string. */
const ALIASES: Record<string, SupportedLanguage> = {
  c: "c",
  h: "c",
  cpp: "c",
  "c++": "c",
  json: "json",
  jsonc: "json",
  bash: "bash",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  console: "bash",
};

export function resolveLanguage(info: string | undefined): SupportedLanguage | null {
  if (!info) return null;
  return ALIASES[info.trim().toLowerCase()] ?? null;
}

type Highlighter = {
  codeToTokens: (
    code: string,
    options: { lang: string; theme: string },
  ) => { tokens: { content: string; color?: string; fontStyle?: number }[][] };
};

let highlighter: Promise<Highlighter> | null = null;

function loadHighlighter(): Promise<Highlighter> {
  highlighter ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] =
      await Promise.all([
        import("shiki/core"),
        import("shiki/engine/javascript"),
      ]);
    const core = await createHighlighterCore({
      themes: [reqtraceCodeTheme],
      langs: [
        import("shiki/langs/c.mjs"),
        import("shiki/langs/json.mjs"),
        import("shiki/langs/bash.mjs"),
      ],
      // `forgiving` keeps a grammar rule the JS engine cannot compile from
      // taking the whole block down: that line just loses its colour.
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    });
    return core as unknown as Highlighter;
  })();
  return highlighter;
}

/**
 * Tokenise one fenced block. Returns the same `HighlightedToken[][]` shape the
 * Code tab renders, so both use one renderer for a line of code.
 */
export async function highlightSnippet(
  code: string,
  lang: SupportedLanguage,
): Promise<HighlightedToken[][]> {
  const core = await loadHighlighter();
  const { tokens } = core.codeToTokens(code, {
    lang,
    theme: "reqtrace",
  });
  return tokens.map((line) =>
    line.map((token) => {
      // Shiki's FontStyle is a bitmask (Italic 1, Bold 2, Underline 4,
      // Strikethrough 8). `NotSet` is -1, so mask only non-negative values —
      // otherwise -1 reads as every style at once.
      const style = token.fontStyle && token.fontStyle > 0 ? token.fontStyle : 0;
      return {
        content: token.content,
        color: token.color,
        italic: (style & 1) !== 0,
        bold: (style & 2) !== 0,
      };
    }),
  );
}
