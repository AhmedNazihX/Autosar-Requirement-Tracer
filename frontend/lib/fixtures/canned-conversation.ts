/**
 * The canned conversation — F5.2's acceptance vehicle.
 *
 * A realistic event stream that exercises all seven event types from spec §6,
 * including a tool call that errors and a stream that ends without `done`.
 *
 * THIS IS A SEAM, NOT A THROWAWAY MOCK. The renderer these events drive is the
 * renderer the real `POST /chat` stream will drive: same types, same reducer,
 * same components. When WP3 lands, `lib/chat-sources.ts` selects the SSE source
 * instead of this array and not one component changes.
 *
 * Everything checkable here is real, taken from the corpus and the pinned
 * snapshot rather than invented:
 *   - requirement ids, verbatim requirement text, page numbers and page counts
 *     come from the R23-11 PDFs named in projects/autosar-can/project.yaml
 *     (SWS_Can_00011 p.37/131, SWS_CANIF_00381 p.44/228, SWS_CANIF_00005
 *     p.82/228, SWS_CANIF_CONSTR_00001 p.222/228, SWS_CanTp_00002 p.50/108,
 *     SWS_CanSM_00008 p.15/115);
 *   - the code citation is the real CanIf_Transmit at
 *     communication/CanIf/src/CanIf.c:739-859, sha 0943377, whose `@req
 *     CANIF381` annotation sits at line 836;
 *   - ids keep the corpus's own casing exactly. Never normalise them.
 *
 * `bbox` is null on every citation, and that is deliberate rather than lazy:
 * ingestion has not run in this workspace, so no bounding box has been
 * extracted. Inventing coordinates would put a fabricated highlight on screen.
 * The Document tab says what is missing instead.
 *
 * The assistant prose is authored for the fixture. It is the one thing here
 * that a model would otherwise have written, and it is written as markdown —
 * bold, inline code, a blockquote and one fenced C block — because that is what
 * the chat agent's system prompt asks the model for and what the renderer has to
 * survive. The fenced block is a verbatim quotation of CanIf.c 832-841 at the
 * pinned SHA, not an illustration.
 */

import type { ChatEvent, RagStage } from "../events";
import type { Thread } from "../threads";

const CHAT_MODEL = "anthropic/claude-sonnet-4.5";

// `doc` is the document's MANIFEST KEY, exactly as ingestion stores it in
// `Requirement.source_doc` and exactly what `GET /documents/{doc}/view` will
// take — see projects/autosar-can/project.yaml. Not a filename: filenames and
// extensions have no business in a URL, and the key survives a rename.
const GIT_SHA = "09433770bebb8f27a7b480d7c96d814c68ffed3e";

const CAN_DRIVER = {
  doc: "can_driver",
  doc_title: "CAN Driver",
  page_count: 131,
} as const;

const CAN_IF = {
  doc: "can_interface",
  doc_title: "CAN Interface",
  page_count: 228,
} as const;

const CAN_TP = {
  doc: "can_transport",
  doc_title: "CAN Transport Layer",
  page_count: 108,
} as const;

const CAN_SM = {
  doc: "can_statemanager",
  doc_title: "CAN State Manager",
  page_count: 115,
} as const;

/** Split authored prose into the `token` deltas a real stream would send. */
function tokens(text: string): ChatEvent[] {
  // Word-ish chunks: enough granularity that streaming is visible, few enough
  // events that a stored thread stays readable in devtools.
  const parts = text.match(/\S+\s*|\s+/g) ?? [text];
  const chunks: string[] = [];
  for (let i = 0; i < parts.length; i += 3) {
    chunks.push(parts.slice(i, i + 3).join(""));
  }
  return chunks.map((chunk) => ({ type: "token", data: { text: chunk } }));
}

const SEARCH_STAGES: RagStage[] = [
  { step: 1, label: "multi-query", detail: "3 rewrites · RAG-Fusion" },
  { step: 2, label: "self-query", detail: "module=Can  doc_type=requirement" },
  { step: 3, label: "hybrid + RRF", detail: "bm25 42  dense 40  → fused 20" },
  { step: 4, label: "rerank", detail: "gpt-4o-mini listwise → top 5" },
];

export interface CannedExchange {
  user: string;
  events: ChatEvent[];
}

/* ============================================================ exchange 1 === */
/* The flagship datum. Two tools, four citations, a usage line, a clean `done`. */

const EXCHANGE_1: CannedExchange = {
  user: "Who owns the transmit buffer during Can_Write — the driver or the upper layer?",
  events: [
    {
      type: "tool_start",
      data: {
        id: "call_1a",
        tool: "search_requirements",
        args: {
          query: "transmit buffer ownership during Can_Write",
          module: "Can",
        },
      },
    },
    {
      type: "tool_result",
      data: {
        id: "call_1a",
        status: "ok",
        summary: "5 of 20",
        duration_ms: 1912,
        stages: SEARCH_STAGES,
      },
    },
    {
      type: "tool_start",
      data: {
        id: "call_1b",
        tool: "lookup_requirement",
        args: { req_id: "SWS_Can_00011" },
      },
    },
    {
      type: "tool_result",
      data: {
        id: "call_1b",
        status: "ok",
        summary: "SWS_Can_00011",
        duration_ms: 41,
      },
    },
    ...tokens(
      "The **upper layer** owns it. `Can_Write` copies the data out of the " +
        "caller's buffer synchronously, so the driver never keeps a reference — " +
        "but the caller must keep that buffer consistent until the call " +
        "returns.\n\n" +
        "SWS_Can_00011 states it directly:\n\n" +
        "> The Can module shall directly copy the data from the upper layer " +
        "buffers. It is the responsibility of the upper layer to keep the " +
        "buffer consistent until return of function call (Can_Write).\n\n" +
        "The CAN Interface only takes ownership in one case, and only if it is " +
        "configured to: SWS_CANIF_00381 says that when Can_Write returns " +
        "CAN_BUSY and transmit buffering is enabled, CanIf checks whether it can " +
        "buffer the L-PDU itself. With buffering disabled there is nothing " +
        "between PduR and the hardware that holds the payload.",
    ),
    {
      type: "citation",
      data: {
        kind: "requirement",
        req_id: "SWS_Can_00011",
        ...CAN_DRIVER,
        page: 37,
        bbox: null,
        section: "7.5.1 Priority Inversion",
        quote:
          "The Can module shall directly copy the data from the upper layer " +
          "buffers. It is the responsibility of the upper layer to keep the " +
          "buffer consistent until return of function call (Can_Write).",
      },
    },
    {
      type: "citation",
      data: {
        kind: "requirement",
        req_id: "SWS_CANIF_00381",
        ...CAN_IF,
        page: 44,
        bbox: null,
        quote:
          "If transmit buffering is enabled (see [SWS_CANIF_00063]) and if the " +
          "call of Can_Write() for a PDU configured for direct transmission " +
          "returns with CAN_BUSY, CanIf shall check if it is possible to buffer " +
          "the CanIf Tx L-PDU, which was requested to be transmitted via " +
          "Can_Write() in a CanIfTxBuffer.",
      },
    },
    {
      type: "citation",
      data: {
        kind: "upstream",
        req_id: "SRS_SPAL_12075",
        cited_by: "SWS_Can_00011",
        doc: "AUTOSAR_SRS_SPALGeneral",
      },
    },
    {
      type: "citation",
      data: {
        kind: "upstream",
        req_id: "SRS_Can_01059",
        cited_by: "SWS_Can_00011",
        doc: "AUTOSAR_SRS_CAN",
      },
    },
    {
      type: "citation",
      data: {
        kind: "upstream",
        req_id: "SRS_Can_01126",
        cited_by: "SWS_CANIF_00381",
        doc: "AUTOSAR_SRS_CAN",
      },
    },
    {
      type: "usage",
      data: {
        model: CHAT_MODEL,
        prompt_tokens: 3412,
        completion_tokens: 486,
        cost_usd: 0.0132,
        elapsed_ms: 4800,
      },
    },
    { type: "done", data: { finish_reason: "stop" } },
  ],
};

/* ============================================================ exchange 2 === */
/* An evidence check. Produces the code citation the Code tab opens at. */

const EXCHANGE_2: CannedExchange = {
  user: "Is SWS_CANIF_00381 actually implemented in the snapshot?",
  events: [
    {
      type: "tool_start",
      data: {
        id: "call_2a",
        tool: "check_implementation",
        args: { req_id: "SWS_CANIF_00381" },
      },
    },
    {
      type: "tool_result",
      data: {
        id: "call_2a",
        status: "ok",
        summary: "partial · 0.61",
        duration_ms: 2840,
      },
    },
    ...tokens(
      "**Partial**, confidence 0.61.\n\n" +
        "`CanIf_Transmit` at CanIf.c:739-859 carries the tier-1 annotation " +
        "`@req CANIF381` on line 836, and the CAN_BUSY branch does exactly what " +
        "the requirement describes — it calls `qReplaceOrAdd` into the " +
        "configured CanIfTxBuffer:\n\n" +
        // Verbatim from CanIf.c 832-841 at the pinned SHA — the same slice the
        // Code tab renders, quoted rather than paraphrased.
        "```c\n" +
        "if( CAN_BUSY == writeRet ) {\n" +
        "    ret = E_NOT_OK;\n" +
        "\n" +
        "#if (CANIF_PUBLIC_TX_BUFFERING == STD_ON)\n" +
        "    /* @req CANIF381 */\n" +
        "    if( (0 != txPduPtr->CanIfTxPduBufferRef->CanIfBufferSize) &&\n" +
        "        (CANIF_HANDLE_TYPE_BASIC == txPduPtr->CanIfTxPduBufferRef->CanIfBufferHthRef->CanIfHthType) ) {\n" +
        "        ret = qReplaceOrAdd(txPduPtr->CanIfTxPduBufferRef, &canPdu, TRUE);\n" +
        "    }\n" +
        "#endif\n" +
        "```\n\n" +
        "Two things stop this being *implemented*. The whole buffering branch " +
        "sits behind that `#if`, so a build with `CANIF_PUBLIC_TX_BUFFERING` off " +
        "satisfies nothing. And the same function carries three negative " +
        "annotations — `!req CANIF323`, `!req CANIF666`, `!req CANIF058` — which " +
        "is the code stating that parts of the transmit contract are not covered " +
        "in this release.",
    ),
    {
      type: "citation",
      data: {
        kind: "code",
        repo_path: "communication/CanIf/src/CanIf.c",
        symbol: "CanIf_Transmit",
        line_span: [739, 859],
        git_sha: GIT_SHA,
      },
    },
    {
      type: "citation",
      data: {
        kind: "requirement",
        req_id: "SWS_CANIF_00005",
        ...CAN_IF,
        page: 82,
        bbox: null,
        quote:
          "Definition of API function CanIf_Transmit. Service ID [hex] 0x49. " +
          "Sync/Async: Synchronous. Reentrancy: Reentrant for different PduIds. " +
          "Non reentrant for the same PduId.",
      },
    },
    {
      type: "usage",
      data: {
        model: CHAT_MODEL,
        prompt_tokens: 5108,
        completion_tokens: 402,
        cost_usd: 0.0187,
        elapsed_ms: 6100,
      },
    },
    { type: "done", data: { finish_reason: "stop" } },
  ],
};

/* ============================================================ exchange 3 === */
/* Long ids and the other two documents, so the 22-character
 * SWS_CANIF_CONSTR_00001 gets exercised against the real chip row. */

const EXCHANGE_3: CannedExchange = {
  user: "Which requirements in CanTp and CanSM touch the same buffer contract?",
  events: [
    {
      type: "tool_start",
      data: {
        id: "call_3a",
        tool: "search_requirements",
        args: {
          query: "buffer contract naming and export conventions",
          module: null,
        },
      },
    },
    {
      type: "tool_result",
      data: {
        id: "call_3a",
        status: "ok",
        summary: "3 of 20",
        duration_ms: 2204,
        stages: [
          { step: 1, label: "multi-query", detail: "3 rewrites · RAG-Fusion" },
          {
            step: 2,
            label: "self-query",
            detail: "no module filter  doc_type=requirement",
          },
          {
            step: 3,
            label: "hybrid + RRF",
            detail: "bm25 61  dense 58  → fused 20",
          },
          { step: 4, label: "rerank", detail: "gpt-4o-mini listwise → top 3" },
        ],
      },
    },
    ...tokens(
      "None of them restate the ownership rule — it lives in the CAN Driver and " +
        "the CAN Interface only. What the other two modules add is adjacent:\n\n" +
        "SWS_CanTp_00002 fixes the type-naming convention CanTp must use for any " +
        "additional types it defines, which is how a CanTp buffer type would be " +
        "named. SWS_CanSM_00008 requires CanSM.h to export the module's types and " +
        "its two APIs. SWS_CANIF_CONSTR_00001 is a configuration constraint " +
        "rather than a behavioural one: it pins how the CAN XL priority id is " +
        "supplied per receive PDU.\n\n" +
        "So the buffer contract itself is not duplicated anywhere below CanIf.",
    ),
    {
      type: "citation",
      data: {
        kind: "requirement",
        req_id: "SWS_CanTp_00002",
        ...CAN_TP,
        page: 50,
        bbox: null,
        quote:
          "If, for implementation reasons, some additional types have to be " +
          "defined, the CanTp module shall label these types as follows: " +
          "CanTp_<TypeName> Type.",
      },
    },
    {
      type: "citation",
      data: {
        kind: "requirement",
        req_id: "SWS_CanSM_00008",
        ...CAN_SM,
        page: 15,
        bbox: null,
        quote:
          "The header file CanSM.h shall export CanSM module specific types and " +
          "the APIs CanSM_GetVersionInfo and CanSM_Init.",
      },
    },
    {
      type: "citation",
      data: {
        kind: "requirement",
        req_id: "SWS_CANIF_CONSTR_00001",
        ...CAN_IF,
        page: 222,
        bbox: null,
        quote:
          "For each CanIfRxPduCfg that contains CanIfRxPduXLParams, the CAN XL " +
          "parameter Priority ID shall either be configured as MetaData item of " +
          "type PRIORITYID_16 for the global PDU referenced via CanIfRxPduRef, or " +
          "as CanIfRxPduXLParams.CanIfXLPriorityId.",
      },
    },
    {
      type: "citation",
      data: {
        kind: "upstream",
        req_id: "SRS_BSW_00358",
        cited_by: "SWS_CanTp_00002",
        doc: "AUTOSAR_SRS_BSWGeneral",
      },
    },
    {
      type: "citation",
      data: {
        kind: "upstream",
        req_id: "SRS_Can_02003",
        cited_by: "SWS_CANIF_CONSTR_00001",
        doc: "AUTOSAR_SRS_CAN",
      },
    },
    {
      type: "usage",
      data: {
        model: CHAT_MODEL,
        prompt_tokens: 4406,
        completion_tokens: 371,
        cost_usd: 0.0154,
        elapsed_ms: 5400,
      },
    },
    { type: "done", data: { finish_reason: "stop" } },
  ],
};

/* ============================================================ exchange 4 === */
/* A tool that ERRORS, followed by an `error` event: destructive-tinted inline
 * message plus a toast. Whatever streamed before the error is kept, so the
 * thread stays consistent instead of half-rendered. */

const EXCHANGE_4: CannedExchange = {
  user: "Run a traceability report over the whole Can module.",
  events: [
    {
      type: "tool_start",
      data: {
        id: "call_4a",
        tool: "generate_traceability_report",
        args: { scope: "module", module: "Can", rejudge: true },
      },
    },
    {
      type: "tool_result",
      data: {
        id: "call_4a",
        status: "error",
        summary: "stopped at the cost ceiling",
        duration_ms: 18420,
        error:
          "The run reached MAX_REPORT_COST_USD ($2.00) after judging 167 of 240 " +
          "requirements. The 167 verdicts it already produced are kept.",
      },
    },
    ...tokens(
      "The run stopped at the cost ceiling before it finished the module. It " +
        "judged 167 of 240 requirements and kept those verdicts, so re-running " +
        "with re-judge off will reuse them and cost far less.",
    ),
    {
      type: "error",
      data: {
        message:
          "The answer stopped mid-stream. OpenRouter returned 502 after 1.2 s. " +
          "Your question and the tokens received so far are saved in the thread.",
        code: "upstream_502",
        retryable: true,
      },
    },
  ],
};

/* ============================================================ exchange 5 === */
/* A stream that simply ends: a `tool_start` with no matching `tool_result`, no
 * usage, no `done`, no `error`. The chip has to say so rather than spin. */

const EXCHANGE_5: CannedExchange = {
  user: "And where is CanIf_RxIndication defined?",
  events: [
    {
      type: "tool_start",
      data: {
        id: "call_5a",
        tool: "search_code",
        args: { symbol: "CanIf_RxIndication" },
      },
    },
    ...tokens("Looking through the indexed snapshot for that symbol"),
  ],
};

export const CANNED_EXCHANGES: CannedExchange[] = [
  EXCHANGE_1,
  EXCHANGE_2,
  EXCHANGE_3,
  EXCHANGE_4,
  EXCHANGE_5,
];

/** Stable id so re-seeding replaces the demo thread instead of duplicating it. */
export const DEMO_THREAD_ID = "th_demo_canned";

/**
 * The demo thread, stored exactly as a real one: every assistant message keeps
 * its full event array, which is what makes reload an exact replay (spec §11).
 */
export function buildDemoThread(): Thread {
  // Fixed timestamps so the sidebar's relative times are stable across reloads
  // and the seeded thread does not jump around the list.
  const base = Date.parse("2026-08-26T14:05:00Z");
  const messages: Thread["messages"] = [];
  let previousId: string | null = null;

  CANNED_EXCHANGES.forEach((exchange, index) => {
    const at = (offset: number) =>
      new Date(base + index * 120_000 + offset).toISOString();

    const userId = `${DEMO_THREAD_ID}_u${index}`;
    messages.push({
      id: userId,
      role: "user",
      content: exchange.user,
      events: [],
      created_at: at(0),
      parent_id: previousId,
    });

    const assistantId = `${DEMO_THREAD_ID}_a${index}`;
    messages.push({
      id: assistantId,
      role: "assistant",
      content: exchange.events
        .filter((event) => event.type === "token")
        .map((event) => event.data.text)
        .join(""),
      events: exchange.events,
      created_at: at(30_000),
      parent_id: userId,
    });
    previousId = assistantId;
  });

  return {
    id: DEMO_THREAD_ID,
    title: "Buffer ownership in Can_Write",
    created_at: new Date(base).toISOString(),
    updated_at: new Date(base + CANNED_EXCHANGES.length * 120_000).toISOString(),
    messages,
  };
}
