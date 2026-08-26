"use client";

import { CoinsIcon } from "lucide-react";

import type { UsageEventData } from "@/lib/events";

/**
 * The per-message token/cost line: one 10.5 px mono row carrying model, tokens
 * in/out, cost and wall time. The whole token-and-cost bonus in 20 px of
 * vertical space (canvas artboard 1).
 *
 * It is rendered from the `usage` event, so it survives a reload for free.
 */
export function UsageLine({ usage }: { usage: UsageEventData }) {
  return (
    <div className="flex items-center gap-1.5 font-mono text-[10.5px] leading-[15px] text-muted-foreground">
      <CoinsIcon className="size-3 flex-none" />
      <span className="truncate">
        {usage.model} · {usage.prompt_tokens.toLocaleString("en-US")} in ·{" "}
        {usage.completion_tokens.toLocaleString("en-US")} out ·{" "}
        {formatCost(usage.cost_usd)} · {(usage.elapsed_ms / 1000).toFixed(1)} s
      </span>
    </div>
  );
}

/**
 * Four decimals, because a single answer costs about a cent and rounding to two
 * would print `$0.01` for everything — the number has to be able to differ
 * between a lookup and a report.
 */
function formatCost(usd: number): string {
  return `$${usd.toFixed(4)}`;
}
