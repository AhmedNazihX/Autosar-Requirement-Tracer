"use client";

/**
 * The *other* half of what the source pane is showing.
 *
 * A citation points at one thing, but the two panes want both: open a
 * requirement and the code implementing it belongs in the Code tab; open a
 * piece of code and the requirement it is tied to belongs in the Document tab.
 * `SourceTarget.companion` is where that second half lives.
 *
 * **Why this is a hook rather than part of `targetForEvents`.** Spec §11 makes
 * the pane a replay of stored events, so `lib/source-target.ts` is a pure
 * function over the citation array and cannot fetch anything. The companion is
 * not in the events — it is a database link — so resolving it has to happen
 * outside that function, and the only honest place is here.
 *
 * **The bug this fixes.** The lookup used to live in `openCitation` alone, so
 * only a *clicked* citation filled the other tab. A turn whose citations are
 * all code — which `search_code` always produces, since it is the one tool
 * that cites no requirement — opened the pane on the Code tab with an empty
 * Document tab, even though the backend knew 47 requirements naming the
 * symbol. "Where is CanIf_RxIndication defined?" showed the code and no PDF.
 * Deriving it from whatever the pane is actually pointed at means both paths,
 * clicked and automatic, behave the same way.
 *
 * Free on the backend: annotations and named symbols are SQL, and a verdict is
 * returned only when already cached, so this cannot start a judge call however
 * often the pane changes. Both fetchers no-op in canned mode
 * (`chatSourceKind()`), so the offline demo stays offline.
 */

import { useEffect, useState } from "react";

import type { CodeCitation, RequirementCitation } from "@/lib/events";
import {
  bestCodeLink,
  bestRequirementLink,
  citationForLink,
  fetchCodeRequirements,
  fetchImplementation,
} from "@/lib/requirements";
import type { SourceTarget } from "@/lib/source-target";

type Companion = RequirementCitation | CodeCitation | null;

/** Identity of the thing being shown, so a stale fetch can be discarded. */
function keyOf(citation: RequirementCitation | CodeCitation): string {
  return citation.kind === "code"
    ? `code:${citation.repo_path}:${citation.line_span[0]}-${citation.line_span[1]}`
    : `req:${citation.req_id}`;
}

async function resolve(
  citation: RequirementCitation | CodeCitation,
): Promise<Companion> {
  if (citation.kind === "code") {
    const links = await fetchCodeRequirements(
      citation.repo_path,
      citation.line_span,
    );
    // Never open a `!req` link: it says the developers believe this code does
    // *not* implement that requirement, so leading with it would present a
    // documented absence as evidence.
    const best = bestRequirementLink(links);
    return best ? citationForLink(best) : null;
  }

  const implementation = await fetchImplementation(citation.req_id);
  const link = bestCodeLink(implementation);
  return link
    ? {
        kind: "code",
        repo_path: link.repo_path,
        symbol: link.symbol,
        line_span: link.line_span,
        git_sha: link.git_sha,
      }
    : null;
}

export function useSourceCompanion(target: SourceTarget | null): Companion {
  // Keyed by the citation that produced it, so one file never shows the
  // previous one's companion while a fetch is in flight, and no state is
  // written during render.
  const [result, setResult] = useState<{
    token: string;
    companion: Companion;
  } | null>(null);

  const citation = target?.citation ?? null;
  const token = citation ? keyOf(citation) : "";
  // A target that already carries its companion needs no lookup — a report row
  // supplies both halves outright (story S5.5.3).
  const wanted = citation !== null && !target?.companion;

  useEffect(() => {
    if (!wanted || !citation) return;
    let cancelled = false;
    void resolve(citation).then((companion) => {
      if (!cancelled) setResult({ token, companion });
    });
    return () => {
      cancelled = true;
    };
  }, [wanted, citation, token]);

  return wanted && result?.token === token ? result.companion : null;
}
