/**
 * Turning a requirement id into a citation the source pane can open.
 *
 * The chat gets citations pushed to it as `citation` events, fully formed. The
 * report matrix does not: a row carries the id, the document key and the page,
 * but not the bbox, the document title or its page count — those live in
 * storage, not in the verdict. `GET /requirements/{req_id}` (story S3.3.2)
 * returns all of it in one call, so a row click resolves rather than guesses.
 *
 * Deliberately produces the same `RequirementCitation` the chat produces. The
 * source pane must not be able to tell where a target came from, or the two
 * paths will drift.
 */

import { chatSourceKind } from "./chat-sources";
import type { RequirementCitation } from "./events";

interface RequirementResponse {
  req_id: string;
  title: string | null;
  text: string;
  section_path: string | null;
  page: number;
  doc: string;
  doc_title: string;
  page_count: number;
  bbox: [number, number, number, number] | null;
  upstream_ids: string[];
  named_symbols: string[];
  version: string;
}

export async function fetchRequirementCitation(
  reqId: string,
): Promise<RequirementCitation | null> {
  try {
    const response = await fetch(
      `/api/py/requirements/${encodeURIComponent(reqId)}`,
    );
    if (!response.ok) return null;
    const data = (await response.json()) as RequirementResponse;
    return {
      kind: "requirement",
      req_id: data.req_id,
      doc: data.doc,
      doc_title: data.doc_title,
      page: data.page,
      bbox: data.bbox,
      page_count: data.page_count,
      // `section` is optional on the wire, not nullable — an absent section is
      // an absent field, matching what a `citation` event sends.
      section: data.section_path ?? undefined,
      quote: data.text,
    };
  } catch {
    return null;
  }
}

/* ------------------------------------------------------- implementation --- */

/** Mirrors `ImplementationLink` in `api/documents.py`. */
export interface ImplementationLink {
  repo_path: string;
  symbol: string;
  kind: string;
  line_span: [number, number];
  git_sha: string;
  found_by: "annotation" | "symbol";
  claim: "claimed_implemented" | "claimed_not_implemented" | null;
  annotation_lines: number[];
}

export interface Implementation {
  req_id: string;
  git_sha: string;
  links: ImplementationLink[];
  verdict: { status: string; confidence: number; rationale?: string } | null;
}

/**
 * The code tied to a requirement, so opening a citation can open both halves.
 *
 * Free on the backend — annotations and named symbols are plain SQL, and a
 * verdict comes back only if one was already cached. Opening a citation must
 * never start a judge call, so this cannot cost anything however often it is
 * called.
 *
 * Returns `null` on any failure: the document half of the citation is already
 * on screen and must not be taken down because the code half could not be
 * resolved.
 */
export async function fetchImplementation(
  reqId: string,
): Promise<Implementation | null> {
  // Canned mode has no backend to ask, and a doomed request per citation
  // click is noise in the one demo that is supposed to run on nothing.
  if (chatSourceKind() !== "live") return null;
  try {
    const response = await fetch(
      `/api/py/requirements/${encodeURIComponent(reqId)}/implementation`,
    );
    if (!response.ok) return null;
    return (await response.json()) as Implementation;
  } catch {
    return null;
  }
}

/**
 * The link a citation click should open in the code tab, or `null`.
 *
 * The backend already orders links by strength of claim, so this is the first
 * one — with one exception it must not get wrong: a link whose only claim is
 * `!req` says the developers believe the requirement is *not* implemented
 * there. Opening that as "the implementation" would invert its meaning, so it
 * is never auto-opened. It stays in `links` for anyone who wants to look.
 */
export function bestCodeLink(
  implementation: Implementation | null,
): ImplementationLink | null {
  const usable = implementation?.links.filter(
    (link) => link.claim !== "claimed_not_implemented",
  );
  return usable?.[0] ?? null;
}
