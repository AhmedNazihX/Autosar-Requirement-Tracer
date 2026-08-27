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
