import type { LineAnnotation } from "@/lib/code-facts";
import type { HighlightedToken } from "@/lib/code-highlight";

/**
 * One line of Shiki output. Shared by the Code tab and by fenced code blocks in
 * chat, which is what makes a C snippet look the same in an answer as it does in
 * the file — both are the same tokens coloured by the same `--code-*` variables.
 *
 * `color` is a `var(--code-*)` reference straight from `reqtraceCodeTheme`, so
 * the line re-themes with the app and nothing here has a palette of its own.
 *
 * `annotation` draws the `@req`/`!req` marker as a bordered chip inside the
 * line (canvas artboard 3). Only the Code tab passes it — its lines carry the
 * lexical scan from `annotationOf` — a chat fence has no line facts and stays
 * plain tokens.
 */
const CHIP_CLASS: Record<LineAnnotation["polarity"], string> = {
  positive: "border-source-border text-source",
  negative: "border-verdict-partial-border text-verdict-partial",
};

export function CodeTokens({
  tokens,
  annotation = null,
}: {
  tokens: readonly HighlightedToken[];
  annotation?: LineAnnotation | null;
}) {
  if (!annotation) return <Plain tokens={tokens} />;
  // The chip replaces the tokens under the marker's column span. Tokens
  // concatenate to exactly the raw line, so clipping by column is lossless —
  // a token straddling the boundary is split, not dropped.
  return (
    <>
      <Plain tokens={clip(tokens, 0, annotation.start)} />
      <span
        className={`inline-block rounded-[4px] border px-1 leading-[15px] font-medium ${CHIP_CLASS[annotation.polarity]}`}
      >
        {annotation.text}
      </span>
      <Plain tokens={clip(tokens, annotation.end, Infinity)} />
    </>
  );
}

function Plain({ tokens }: { tokens: readonly HighlightedToken[] }) {
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

/** The tokens covering columns `[from, to)`, split mid-token where needed. */
function clip(
  tokens: readonly HighlightedToken[],
  from: number,
  to: number,
): HighlightedToken[] {
  const out: HighlightedToken[] = [];
  let column = 0;
  for (const token of tokens) {
    const start = Math.max(column, from);
    const end = Math.min(column + token.content.length, to);
    if (end > start) {
      out.push({ ...token, content: token.content.slice(start - column, end - column) });
    }
    column += token.content.length;
  }
  return out;
}
