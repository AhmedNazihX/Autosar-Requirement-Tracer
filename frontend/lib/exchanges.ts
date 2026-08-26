/**
 * Grouping a thread's flat message list into exchanges — one user request plus
 * the assistant message that answered it.
 *
 * PURE and dependency-free, like the reducer. It exists because two very
 * different views need the same grouping and must not disagree: the transcript
 * renders one block per exchange, and the checkpoint rail draws one dot per
 * exchange (spec §11: "one dot per user request"). Deriving that twice would
 * eventually put the dots out of step with the turns they point at.
 */

import { firstToolOf } from "./event-reducer";
import type { ToolName } from "./events";
import { targetForEvents, type SourceTarget } from "./source-target";
import type { StoredMessage } from "./threads";

export interface Exchange {
  /** Stable DOM/scroll anchor. */
  id: string;
  /** 1-based turn number, as the rail's tooltip prints it. */
  turn: number;
  user: StoredMessage | null;
  assistant: StoredMessage | null;
  /** Drives the rail's icon. `null` when the turn called no tool. */
  tool: ToolName | null;
  /** What the source pane showed for this turn, for the rail's tooltip. */
  target: SourceTarget | null;
}

export function toExchanges(messages: readonly StoredMessage[]): Exchange[] {
  const exchanges: Exchange[] = [];

  for (const message of messages) {
    const last = exchanges.at(-1);
    // A user message always opens a turn. An assistant message joins the open
    // turn, unless it already has an answer or there is no turn yet — which is
    // what a mid-thread reload or a future branch would look like.
    if (message.role === "user" || !last || last.assistant) {
      exchanges.push({
        id: message.id,
        turn: exchanges.length + 1,
        user: message.role === "user" ? message : null,
        assistant: message.role === "assistant" ? message : null,
        tool: firstToolOf(message.events),
        target: targetForEvents(message.events),
      });
      continue;
    }

    last.assistant = message;
    last.tool = firstToolOf(message.events);
    last.target = targetForEvents(message.events);
  }

  return exchanges;
}
