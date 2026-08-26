"use client";

import { useEffect, useState } from "react";

import type { HighlightedToken } from "@/lib/code-highlight";
import {
  highlightSnippet,
  resolveLanguage,
  type SupportedLanguage,
} from "@/lib/code-highlight-client";

import { CodeTokens } from "./code-tokens";

/**
 * A fenced code block inside an assistant message.
 *
 * Highlighted by Shiki, not by react-markdown's default renderer, so code in an
 * answer matches the Code tab exactly. The highlighter arrives in a lazy chunk,
 * so the block renders as plain monospace first and gains colour when the
 * grammar has loaded — never a spinner, and never a layout shift, because the
 * unhighlighted text occupies the same box.
 *
 * A fence whose language is not one of the three we carry stays plain. That is
 * deliberate: colouring C as if it were Python would be worse than not
 * colouring it.
 */
export function ChatCodeBlock({
  code,
  info,
}: {
  code: string;
  info: string | undefined;
}) {
  const lang = resolveLanguage(info);
  const lines = useHighlightedSnippet(code, lang);

  return (
    <div className="my-2.5 overflow-hidden rounded-lg border bg-code-bg">
      {info ? (
        <div className="flex h-6 items-center border-b px-2.5">
          <span className="font-mono text-[10.5px] text-muted-foreground">
            {info}
          </span>
        </div>
      ) : null}
      <pre className="overflow-x-auto px-2.5 py-2 font-mono text-[11.8px] leading-[17px]">
        <code>
          {lines
            ? lines.map((tokens, index) => (
                <span key={index} className="block">
                  <CodeTokens tokens={tokens} />
                  {"\n"}
                </span>
              ))
            : code}
        </code>
      </pre>
    </div>
  );
}

/**
 * `null` until the grammar is loaded, or forever when the language is not one we
 * carry. A highlighter failure degrades to plain text rather than an error: a
 * code fence in an answer is content, not a feature that can fail.
 */
function useHighlightedSnippet(
  code: string,
  lang: SupportedLanguage | null,
): HighlightedToken[][] | null {
  const [lines, setLines] = useState<HighlightedToken[][] | null>(null);

  useEffect(() => {
    if (!lang) return;
    let cancelled = false;
    highlightSnippet(code, lang)
      .then((result) => {
        if (!cancelled) setLines(result);
      })
      .catch(() => {
        if (!cancelled) setLines(null);
      });
    return () => {
      cancelled = true;
    };
  }, [code, lang]);

  // Gated on `lang` rather than cleared in the effect, so an unsupported fence
  // never renders a previous block's colours.
  return lang ? lines : null;
}
