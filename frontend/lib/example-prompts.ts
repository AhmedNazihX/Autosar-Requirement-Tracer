import type { ToolName } from "./events";

/**
 * One example question per tool — the shared vocabulary between the
 * empty-thread state (canvas artboard 5, cell C) and the help guide, so the
 * two never teach different phrasings for the same tool.
 */
export interface ExamplePrompt {
  prompt: string;
  tool: ToolName;
}

export const EXAMPLE_PROMPTS: ExamplePrompt[] = [
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
  {
    prompt: "Generate a traceability report for CanIf",
    tool: "generate_traceability_report",
  },
];
