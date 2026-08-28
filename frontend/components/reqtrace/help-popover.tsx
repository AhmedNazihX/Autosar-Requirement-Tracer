"use client";

import { CircleHelpIcon } from "lucide-react";

import { EXAMPLE_PROMPTS } from "@/lib/example-prompts";
import { CANIF_FIXTURE_SHA } from "@/lib/fixtures/canif-c";
import type { ToolName } from "@/lib/events";
import type { SetupStatus } from "@/hooks/use-setup-status";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Cap, Kbd } from "@/components/reqtrace/text";

import { TOOL_ICON } from "./chat/tool-chip";

/** One line per tool — what it does, in the user's terms. */
const TOOL_BLURB: Record<ToolName, string> = {
  search_requirements:
    "Searches the specifications by meaning — query rewrites, filters, hybrid search and rerank, all visible on the answer's chip.",
  lookup_requirement:
    "Fetches one requirement by its exact ID. A lookup, never a search.",
  search_code:
    "Searches the pinned C snapshot, by exact symbol or by meaning.",
  check_implementation:
    "Judges whether one requirement is implemented in the snapshot, with the file and lines as evidence.",
  generate_traceability_report:
    "Runs a scoped SRS → SWS → code traceability report as a background job.",
};

/**
 * The `?` guide in the header: what ReqTrace does, the five tools with an
 * example question each, the keyboard shortcuts, and the corpus the answers
 * are true against.
 *
 * Driven entirely by the `setup` state the shell already holds — no fetch, so
 * canned mode (where `setup` is null) gets the same guide over static copy.
 */
export function HelpPopover({ setup }: { setup: SetupStatus | null }) {
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button variant="ghost" size="icon-sm" aria-label="Help and guide">
            <CircleHelpIcon />
          </Button>
        }
      />
      <PopoverContent
        align="end"
        className="flex max-h-[min(560px,80vh)] w-[360px] flex-col gap-3 overflow-y-auto"
      >
        <div className="flex flex-col gap-1">
          <h3 className="text-[13px] leading-[17px] font-semibold">
            What ReqTrace does
          </h3>
          <p className="text-[11.5px] leading-[16px] text-muted-foreground">
            Ask about AUTOSAR CAN-stack requirements in plain language. Answers
            cite their sources — click a chip under an answer to open the exact
            page or the code it points at — and can check whether a requirement
            is actually implemented in the indexed snapshot.
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <Cap>The five tools</Cap>
          <div className="flex flex-col gap-2">
            {EXAMPLE_PROMPTS.map((example) => {
              const Icon = TOOL_ICON[example.tool];
              return (
                <div key={example.tool} className="flex gap-2">
                  <Icon className="mt-px size-3.5 flex-none text-muted-foreground" />
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <span className="font-mono text-[11px] leading-[14px] font-medium">
                      {example.tool}
                    </span>
                    <span className="text-[11px] leading-[15px] text-muted-foreground">
                      {TOOL_BLURB[example.tool]}
                    </span>
                    <span className="text-[11px] leading-[15px] text-muted-foreground italic">
                      “{example.prompt}”
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <Cap>Keyboard</Cap>
          <p className="text-[11.5px] leading-[17px] text-muted-foreground">
            <Kbd>Enter</Kbd> sends · <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> inserts a
            newline · <Kbd>Esc</Kbd> closes panels like this one.
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <Cap>The corpus</Cap>
          {setup ? (
            <p className="text-[11.5px] leading-[16px] text-muted-foreground">
              {setup.documents.length} AUTOSAR specifications
              {setup.corpus_version ? ` (${setup.corpus_version})` : ""} —{" "}
              {setup.requirements.toLocaleString()} requirements — traced
              against{" "}
              <span className="font-mono">
                {setup.code_units.toLocaleString()}
              </span>{" "}
              code units pinned at{" "}
              <span className="font-mono">
                {setup.git_sha ? setup.git_sha.slice(0, 7) : "unknown"}
              </span>
              . Answers are true against this snapshot, nothing newer.
            </p>
          ) : (
            <p className="text-[11.5px] leading-[16px] text-muted-foreground">
              AUTOSAR CAN-stack specifications (R23-11), traced against{" "}
              <span className="font-mono">openAUTOSAR/classic-platform</span>{" "}
              at <span className="font-mono">{CANIF_FIXTURE_SHA.slice(0, 7)}</span>.
              Answers are true against that snapshot, nothing newer.
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
