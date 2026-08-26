import type { HighlightedToken } from "@/lib/code-highlight";

/**
 * One line of Shiki output. Shared by the Code tab and by fenced code blocks in
 * chat, which is what makes a C snippet look the same in an answer as it does in
 * the file — both are the same tokens coloured by the same `--code-*` variables.
 *
 * `color` is a `var(--code-*)` reference straight from `reqtraceCodeTheme`, so
 * the line re-themes with the app and nothing here has a palette of its own.
 */
export function CodeTokens({ tokens }: { tokens: readonly HighlightedToken[] }) {
  return (
    <>
      {tokens.map((token, index) => (
        <span
          key={index}
          style={{
            color: token.color,
            fontStyle: token.italic ? "italic" : undefined,
            fontWeight: token.bold ? 600 : undefined,
          }}
        >
          {token.content}
        </span>
      ))}
    </>
  );
}
