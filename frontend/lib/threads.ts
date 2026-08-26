/**
 * Thread data access — the ONLY place threads are read or written.
 *
 * THIS IS A SEAM, NOT A MOCK. The function signatures are the shape of the
 * real API from spec §6:
 *
 *   listThreads()          ->  GET    /api/py/threads
 *   createThread()         ->  POST   /api/py/threads
 *   getThread(id)          ->  GET    /api/py/threads/{id}   (with stored events)
 *   renameThread(id, t)    ->  PATCH  /api/py/threads/{id}
 *   deleteThread(id)       ->  DELETE /api/py/threads/{id}
 *   appendMessage(...)     ->  persisted by POST /api/py/chat
 *
 * Every one is async, so when WP3 lands the bodies below are replaced with
 * `fetch` calls and no component changes. Nothing outside this module touches
 * localStorage, and no component calls `fetch` for thread data.
 *
 * The schema mirrors spec §11: a thread has an id, a title and timestamps; a
 * message has a role, content, and its FULL event array. The event array is
 * what makes reload an exact replay, so it is stored verbatim and never
 * summarised on the way in.
 */

import type { ChatEvent } from "./events";

const STORAGE_KEY = "reqtrace.threads.v1";
const LAYOUT_KEY_PREFIX = "reqtrace.layout.";

export type MessageRole = "user" | "assistant";

export interface StoredMessage {
  id: string;
  role: MessageRole;
  /** For a user message, the text typed. For an assistant message, the text
   *  reconstructed from its `token` events — stored so exports and titles do
   *  not have to re-run the reducer, never used as the render source. */
  content: string;
  /** The complete SSE stream for this message, in arrival order. */
  events: ChatEvent[];
  created_at: string;
  /** Parent linkage kept so branching stays possible later without a migration
   *  (spec §11). Nothing in v1 reads it. */
  parent_id: string | null;
}

export interface Thread {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: StoredMessage[];
}

export interface ThreadSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

/** Default title for a thread with nothing in it yet. WP3 replaces this with
 *  the cheap-model auto-title call (spec §11); until then the first user
 *  message stands in, which is what an auto-title approximates anyway. */
export const UNTITLED = "New thread";

function newId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}_${crypto.randomUUID().slice(0, 12)}`;
  }
  return `${prefix}_${Math.random().toString(36).slice(2, 14)}`;
}

function readAll(): Thread[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed as Thread[];
  } catch {
    // Corrupt or unavailable storage must not take the app down.
    return [];
  }
}

function writeAll(threads: Thread[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(threads));
  } catch {
    // Quota or private-mode failure: the session keeps working, in memory.
  }
}

function summarise(thread: Thread): ThreadSummary {
  return {
    id: thread.id,
    title: thread.title,
    created_at: thread.created_at,
    updated_at: thread.updated_at,
    message_count: thread.messages.length,
  };
}

/** Newest first, which is the order the sidebar renders. */
function byRecency(a: Thread, b: Thread): number {
  return b.updated_at.localeCompare(a.updated_at);
}

export async function listThreads(): Promise<ThreadSummary[]> {
  return readAll().sort(byRecency).map(summarise);
}

export async function getThread(id: string): Promise<Thread | null> {
  return readAll().find((thread) => thread.id === id) ?? null;
}

export async function createThread(title = UNTITLED): Promise<Thread> {
  const now = new Date().toISOString();
  const thread: Thread = {
    id: newId("th"),
    title,
    created_at: now,
    updated_at: now,
    messages: [],
  };
  writeAll([thread, ...readAll()]);
  return thread;
}

export async function renameThread(
  id: string,
  title: string,
): Promise<ThreadSummary | null> {
  const threads = readAll();
  const thread = threads.find((candidate) => candidate.id === id);
  if (!thread) return null;
  const trimmed = title.trim();
  // An empty rename is a no-op, not a nameless thread.
  if (trimmed.length > 0) thread.title = trimmed.slice(0, 120);
  thread.updated_at = new Date().toISOString();
  writeAll(threads);
  return summarise(thread);
}

export async function deleteThread(id: string): Promise<void> {
  writeAll(readAll().filter((thread) => thread.id !== id));
  if (typeof window !== "undefined") {
    try {
      window.localStorage.removeItem(LAYOUT_KEY_PREFIX + id);
    } catch {
      /* ignore */
    }
  }
}

export interface NewMessage {
  role: MessageRole;
  content: string;
  events: ChatEvent[];
  parent_id?: string | null;
}

/**
 * Append one message and return the whole thread, so the caller re-renders from
 * stored state rather than from whatever it happened to have in memory. That is
 * deliberate: it means the live render path and the reload render path go
 * through the same data.
 */
export async function appendMessage(
  threadId: string,
  message: NewMessage,
): Promise<Thread | null> {
  const threads = readAll();
  const thread = threads.find((candidate) => candidate.id === threadId);
  if (!thread) return null;
  const previous = thread.messages.at(-1);
  thread.messages.push({
    id: newId("msg"),
    role: message.role,
    content: message.content,
    events: message.events,
    created_at: new Date().toISOString(),
    parent_id: message.parent_id ?? previous?.id ?? null,
  });
  // First user message stands in for the auto-title until WP3 supplies one.
  if (thread.title === UNTITLED && message.role === "user") {
    thread.title = message.content.trim().slice(0, 72) || UNTITLED;
  }
  thread.updated_at = new Date().toISOString();
  writeAll(threads);
  return thread;
}

/** Replace a thread wholesale. Used only to seed the demo thread on first run. */
export async function putThread(thread: Thread): Promise<void> {
  const others = readAll().filter((candidate) => candidate.id !== thread.id);
  writeAll([thread, ...others]);
}

/* ---------------------------------------------------------------- layout ---- */
/* The chat/source split is stored per thread (canvas `layout-rules` note). It
 * is view state, not thread content, so it stays out of the thread record and
 * out of whatever the backend will store. */

export function getPaneLayout(threadId: string): Record<string, number> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(LAYOUT_KEY_PREFIX + threadId);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    return parsed as Record<string, number>;
  } catch {
    return null;
  }
}

export function setPaneLayout(
  threadId: string,
  layout: Record<string, number>,
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      LAYOUT_KEY_PREFIX + threadId,
      JSON.stringify(layout),
    );
  } catch {
    /* ignore */
  }
}
