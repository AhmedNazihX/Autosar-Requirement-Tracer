"use client";

import { CANIF_FIXTURE_SHA } from "@/lib/fixtures/canif-c";
import type { ToolName } from "@/lib/events";

import { TOOL_ICON } from "./tool-chip";

/**
 * The empty-thread state — canvas artboard 5, cell C.
 *
 * Four prompts, one per tool, so `search_requirements`, `lookup_requirement`,
 * `check_implementation` and `search_code` are all discoverable without
 * documentation. The subhead states the corpus and the pinned SHA, so the
 * boundary of what can be answered is visible before the first question.
 */
const EXAMPLES: { prompt: string; tool: ToolName }[] = [
  {
    prompt: "Who owns the transmit buffer during Can_Write?",
    tool: "search_requirements",
  },
  {
    prompt: "What does SWS_CANIF_00064 require?",
    tool: "lookup_requirement",
  },
  {
    prompt: "Is SWS_CANIF_00381 implemented in the snapshot?",
    tool: "check_implementation",
  },
  {
    prompt: "Where is CanIf_RxIndication defined?",
    tool: "search_code",
  },
];

export function EmptyThread({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col justify-center px-8 py-6">
      <h2 className="text-[17px] leading-[22px] font-semibold tracking-[-0.01em]">
        Ask about a requirement
      </h2>
      <p className="mt-1.5 text-[12.5px] leading-[18px] text-muted-foreground">
        AUTOSAR CAN Driver, CAN Interface, CAN Transport Layer and CAN State
        Manager specifications (R23-11), traced against{" "}
        <span className="font-mono">openAUTOSAR/classic-platform</span> at{" "}
        <span className="font-mono">{CANIF_FIXTURE_SHA.slice(0, 7)}</span>.
      </p>

      <div className="mt-4 flex flex-col gap-2">
        {EXAMPLES.map((example) => {
          const Icon = TOOL_ICON[example.tool];
          return (
            <button
              key={example.tool}
              type="button"
              onClick={() => onPick(example.prompt)}
              className="flex items-center gap-2.5 rounded-lg border bg-card px-3 py-2 text-left transition-colors hover:bg-muted"
            >
              <Icon className="size-3.5 flex-none text-muted-foreground" />
              <span className="min-w-0 flex-1 text-[12.5px] leading-[17px]">
                {example.prompt}
              </span>
              <span className="flex-none font-mono text-[10.5px] text-muted-foreground">
                {example.tool}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
