/**
 * What the source pane is pointed at — derived from `citation` events only.
 *
 * PURE and dependency-free, like `event-reducer.ts`, and for the same reason:
 * spec §11 requires the source pane to be a replay of stored events, so the
 * mapping from an event to a view has to be a function, not a side effect
 * scattered through click handlers.
 *
 * Upstream (SRS/RS) citations map to `null` on purpose. They are never
 * clickable through to a document because SRS documents are not ingested.
 */

import type {
  ChatEvent,
  Citation,
  CodeCitation,
  RequirementCitation,
} from "./events";

export type SourceTab = "document" | "code";

export interface SourceTarget {
  tab: SourceTab;
  citation: RequirementCitation | CodeCitation;
}

/** `null` for an upstream citation: there is no page to open. */
export function targetForCitation(citation: Citation): SourceTarget | null {
  switch (citation.kind) {
    case "requirement":
      return { tab: "document", citation };
    case "code":
      return { tab: "code", citation };
    default:
      return null;
  }
}

/**
 * The view an exchange left the source pane in: its first clickable citation.
 * This is what a checkpoint restore would replay (F5.4) and what a fresh thread
 * load uses to fill the pane instead of leaving it empty.
 */
export function targetForEvents(
  events: readonly ChatEvent[],
): SourceTarget | null {
  for (const event of events) {
    if (event.type !== "citation") continue;
    const target = targetForCitation(event.data);
    if (target) return target;
  }
  return null;
}

/** Stable identity for a target, so effects can depend on "did the view change". */
export function targetKey(target: SourceTarget | null): string {
  if (!target) return "";
  if (target.citation.kind === "requirement") {
    return `document:${target.citation.doc}:${target.citation.req_id}:${target.citation.page}`;
  }
  return `code:${target.citation.repo_path}:${target.citation.line_span[0]}-${target.citation.line_span[1]}`;
}
