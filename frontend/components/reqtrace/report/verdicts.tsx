import type { Verdict } from "@/lib/events";

/**
 * The verdict tokens as Tailwind classes, shared by every report panel.
 *
 * One map, not four copies: the launch form's warnings, the progress tallies,
 * the summary tiles and the matrix badges must all mean the same thing by the
 * same colour, and `globals.css` already defines exactly one token triple per
 * verdict. Nothing in the report UI may colour a verdict any other way.
 */
export const VERDICT_CLASS: Record<Verdict, string> = {
  implemented:
    "border-verdict-implemented-border bg-verdict-implemented-bg text-verdict-implemented",
  partial: "border-verdict-partial-border bg-verdict-partial-bg text-verdict-partial",
  missing: "border-verdict-missing-border bg-verdict-missing-bg text-verdict-missing",
  unverifiable:
    "border-verdict-unverifiable-border bg-verdict-unverifiable-bg text-verdict-unverifiable",
};

/**
 * Solid fills for the stacked distribution bars. `missing` is drawn at 40 %
 * opacity on purpose: in this corpus it is the *expected* outcome (release
 * drift), and a bar that is three-quarters full-strength grey would read as
 * three-quarters failure.
 */
const VERDICT_BAR_CLASS: Record<Verdict, string> = {
  implemented: "bg-verdict-implemented",
  partial: "bg-verdict-partial",
  missing: "bg-verdict-missing opacity-40",
  unverifiable: "bg-verdict-unverifiable",
};

/** Missing last, so the de-emphasised segment trails rather than splits. */
const BAR_ORDER: readonly Verdict[] = [
  "implemented",
  "partial",
  "unverifiable",
  "missing",
];

export type VerdictCounts = Record<Verdict, number>;

/** One stacked distribution bar. Widths are shares of `total`. */
export function VerdictBar({
  counts,
  total,
  className = "h-2",
}: {
  counts: VerdictCounts;
  total: number;
  className?: string;
}) {
  return (
    <div className={`flex overflow-hidden rounded-full bg-muted ${className}`}>
      {BAR_ORDER.map((verdict) =>
        total > 0 && counts[verdict] > 0 ? (
          <div
            key={verdict}
            className={VERDICT_BAR_CLASS[verdict]}
            style={{ width: `${(100 * counts[verdict]) / total}%` }}
          />
        ) : null,
      )}
    </div>
  );
}

/** `0 impl · 6 part · 231 miss · 3 unver` — the tally line the artboard uses. */
export function tallyLine(counts: VerdictCounts): string {
  return `${counts.implemented} impl · ${counts.partial} part · ${counts.missing} miss · ${counts.unverifiable} unver`;
}
