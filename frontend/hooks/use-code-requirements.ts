"use client";

/**
 * The requirements a piece of open code is tied to.
 *
 * The reverse of the link `openCitation` follows in the other direction, and
 * it earns its own hook because the Code tab shows *all* of them rather than
 * just the one the pane opened: a file-header comment block routinely names a
 * dozen requirements, and which one you care about is the reader's decision,
 * not ours.
 *
 * Free on the backend — annotations and named symbols are SQL, and no verdict
 * is computed — so this runs on every code citation without costing anything.
 * Failures return an empty list: the code is already on screen and must not be
 * taken down because its links could not be resolved.
 */

import { useEffect, useState } from "react";

import type { CodeCitation } from "@/lib/events";
import { fetchCodeRequirements, type LinkedRequirement } from "@/lib/requirements";

export function useCodeRequirements(
  citation: CodeCitation | null,
): LinkedRequirement[] {
  // Keyed by the citation that produced it, so a new file never shows the
  // previous file's requirements while the fetch is in flight — and so no
  // state is written during render.
  const [result, setResult] = useState<{
    token: string;
    links: LinkedRequirement[];
  } | null>(null);

  const token = citation
    ? `${citation.repo_path}:${citation.line_span[0]}-${citation.line_span[1]}`
    : "";

  useEffect(() => {
    if (!citation) return;
    let cancelled = false;
    void fetchCodeRequirements(citation.repo_path, citation.line_span).then(
      (links) => {
        if (!cancelled) setResult({ token, links });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [citation, token]);

  return result?.token === token ? result.links : [];
}
