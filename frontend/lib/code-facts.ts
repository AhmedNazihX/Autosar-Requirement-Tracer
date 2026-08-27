/**
 * The two pure facts every code renderer must agree on, defined once.
 *
 * The server highlighter (`code-highlight.ts`, Shiki in a Server Component),
 * the lazy client highlighter (`code-highlight-client.ts`) and the live file
 * hook (`use-code-file.ts`) each carried private copies of these. They are
 * kept in a module of their own — importing them must not pull Shiki into
 * either bundle, which is the entire point of that server/client split.
 */

/** Requirement-annotation polarity of one source line, if any. */
export type LineAnnotation = "positive" | "negative" | null;

/** `@req 4.0.3/CANIF005` and `!req CANIF058` both appear in the real snapshot. */
const POSITIVE_ANNOTATION = /@req\s+\S+/;
const NEGATIVE_ANNOTATION = /!req\s+\S+/;

/**
 * The annotation this line carries — a corpus fact, not a style choice: the
 * markers come from the manifest's annotation pattern, and a change there
 * must reach the fixture view and the live view together.
 */
export function annotationOf(line: string): LineAnnotation {
  // A `!req` on the same line wins: it is the stronger claim.
  if (NEGATIVE_ANNOTATION.test(line)) return "negative";
  if (POSITIVE_ANNOTATION.test(line)) return "positive";
  return null;
}

/**
 * Shiki's `fontStyle` bitmask as the two flags the renderer draws.
 *
 * The mask is Italic 1, Bold 2, Underline 4, Strikethrough 8, so combinations
 * must be masked, not compared — and `NotSet` is -1, which every mask
 * matches, so negatives are floored to 0 rather than read as "italic and
 * bold and underlined".
 */
export function styleFlags(fontStyle: number | undefined): {
  italic: boolean;
  bold: boolean;
} {
  const style = fontStyle && fontStyle > 0 ? fontStyle : 0;
  return { italic: (style & 1) !== 0, bold: (style & 2) !== 0 };
}
