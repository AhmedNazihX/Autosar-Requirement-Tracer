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

/* ------------------------------------------------- code -> requirements --- */

/** Mirrors `LinkedRequirement` in `api/documents.py`. */
export interface LinkedRequirement {
  req_id: string;
  doc: string;
  doc_title: string;
  page: number;
  page_count: number;
  bbox: [number, number, number, number] | null;
  section: string | null;
  quote: string;
  found_by: "annotation" | "symbol";
  claim: "claimed_implemented" | "claimed_not_implemented" | null;
  via_symbol: string;
  via_kind: string;
}

/**
 * The requirements a piece of code is tied to — the reverse of
 * {@link fetchImplementation}.
 *
 * Free and verdict-free on the backend, same as the forward direction. One
 * code unit routinely names several requirements (a file-header comment block
 * can carry a dozen), so this returns all of them and the caller decides what
 * to show.
 */
export async function fetchCodeRequirements(
  repoPath: string,
  lineSpan: [number, number],
): Promise<LinkedRequirement[]> {
  if (chatSourceKind() !== "live") return [];
  try {
    const path = repoPath.split("/").map(encodeURIComponent).join("/");
    const response = await fetch(
      `/api/py/code/${path}/requirements?lines=${lineSpan[0]}-${lineSpan[1]}`,
    );
    if (!response.ok) return [];
    const body = (await response.json()) as { requirements: LinkedRequirement[] };
    return body.requirements;
  } catch {
    return [];
  }
}

/** A linked requirement as the citation the source pane opens. */
export function citationForLink(link: LinkedRequirement): RequirementCitation {
  return {
    kind: "requirement",
    req_id: link.req_id,
    doc: link.doc,
    doc_title: link.doc_title,
    page: link.page,
    bbox: link.bbox,
    page_count: link.page_count,
    section: link.section ?? undefined,
    quote: link.quote,
  };
}

/**
 * The requirement a code citation should open, or `null`.
 *
 * Never a `!req` link: that comment says the developers believe the
 * requirement is *not* implemented here, so opening it as "what this code
 * implements" would invert its meaning. It stays in the list, labelled.
 */
export function bestRequirementLink(
  links: readonly LinkedRequirement[],
): LinkedRequirement | null {
  return links.find((link) => link.claim !== "claimed_not_implemented") ?? null;
}
