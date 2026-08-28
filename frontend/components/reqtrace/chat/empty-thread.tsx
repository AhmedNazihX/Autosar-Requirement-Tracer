"use client";

import { CANIF_FIXTURE_SHA } from "@/lib/fixtures/canif-c";
import { EXAMPLE_PROMPTS } from "@/lib/example-prompts";

import { TOOL_ICON } from "./tool-chip";

/**
 * The empty-thread state — canvas artboard 5, cell C.
 *
 * Four prompts so `search_requirements`, `lookup_requirement`,
 * `check_implementation` and `search_code` are all discoverable without
 * documentation. The report prompt stays out on purpose: launching a paid
 * background job is not a first question, and this cell has always shown the
 * four question-shaped prompts. The subhead states the corpus and the pinned
 * SHA, so the boundary of what can be answered is visible before the first
 * question.
 */
const EXAMPLES = EXAMPLE_PROMPTS.filter(
  (example) => example.tool !== "generate_traceability_report",
);

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
