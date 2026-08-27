/**
 * Thread data access — the ONLY place threads are read or written.
 *
 * Two backings, one set of exports (story S5.1.2):
 *
 *   listThreads()          ->  GET    /api/py/threads
 *   createThread()         ->  POST   /api/py/threads
 *   getThread(id)          ->  GET    /api/py/threads/{id}   (with stored events)
 *   renameThread(id, t)    ->  PATCH  /api/py/threads/{id}
 *   deleteThread(id)       ->  DELETE /api/py/threads/{id}
 *   appendMessage(...)     ->  already persisted by POST /api/py/chat; re-reads
 *
 * The signatures were written as a seam in WP1 and have not changed — the
 * localStorage bodies moved into `localStore` and an `apiStore` was added
 * beside them. No component changed.
 *
 * **The backing follows `chatSourceKind()`, deliberately.** Threads and chat
 * must agree about which world they are in: a live chat writing turns into
 * SQLite while the sidebar reads localStorage would show a conversation that
 * vanishes on reload, and a canned chat writing into a live thread would
 * persist a fixture as if it were real. One switch, both.
 *
 * The schema mirrors spec §11: a thread has an id, a title and timestamps; a
 * message has a role, content, and its FULL event array. The event array is
 * what makes reload an exact replay, so it is stored verbatim and never
 * summarised on the way in. It matches `api/threads.py`'s `Thread` /
 * `StoredMessage` field for field — that is the contract, not a coincidence,
 * and the two must be changed together.
 */

import { chatSourceKind } from "./chat-sources";
import { isChatEvent, type ChatEvent } from "./events";

const STORAGE_KEY = "reqtrace.threads.v1";
const LAYOUT_KEY_PREFIX = "reqtrace.layout.";
const ACTIVE_KEY = "reqtrace.activeThread";

/** Every thread call goes through the Next proxy — no CORS, ever (spec §7). */
const API = "/api/py/threads";

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

/** Default title for a thread with nothing in it yet. In live mode the backend
 *  replaces it with the cheap-model auto-title (story S3.5.3) as soon as the
 *  first message lands; in canned mode the first user message stands in, which
 *  is what an auto-title approximates anyway. */
export const UNTITLED = "New thread";

export interface NewMessage {
  role: MessageRole;
  content: string;
  events: ChatEvent[];
  parent_id?: string | null;
}

interface ThreadStore {
  list(): Promise<ThreadSummary[]>;
  get(id: string): Promise<Thread | null>;
  create(title: string): Promise<Thread>;
  rename(id: string, title: string): Promise<ThreadSummary | null>;
  remove(id: string): Promise<void>;
  append(threadId: string, message: NewMessage): Promise<Thread | null>;
  put(thread: Thread): Promise<void>;
}

function newId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}_${crypto.randomUUID().slice(0, 12)}`;
  }
  return `${prefix}_${Math.random().toString(36).slice(2, 14)}`;
}

/* ------------------------------------------------------------- local ------- */
/* The canned world. Also the fallback the app runs in with no backend at all,
 * which is what makes the committed demo conversation openable on a fresh
 * clone with no key. */

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

const localStore: ThreadStore = {
  async list() {
    return readAll().sort(byRecency).map(summarise);
  },

  async get(id) {
    return readAll().find((thread) => thread.id === id) ?? null;
  },

  async create(title) {
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
  },

  async rename(id, title) {
    const threads = readAll();
    const thread = threads.find((candidate) => candidate.id === id);
    if (!thread) return null;
    const trimmed = title.trim();
    // An empty rename is a no-op, not a nameless thread.
    if (trimmed.length > 0) thread.title = trimmed.slice(0, 120);
    thread.updated_at = new Date().toISOString();
    writeAll(threads);
    return summarise(thread);
  },

  async remove(id) {
    writeAll(readAll().filter((thread) => thread.id !== id));
    forgetLayout(id);
  },

  async append(threadId, message) {
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
    // With no backend there is no auto-title call, so the first user message
    // stands in for one.
    if (thread.title === UNTITLED && message.role === "user") {
      thread.title = message.content.trim().slice(0, 72) || UNTITLED;
    }
    thread.updated_at = new Date().toISOString();
    writeAll(threads);
    return thread;
  },

  async put(thread) {
    const others = readAll().filter((candidate) => candidate.id !== thread.id);
    writeAll([thread, ...others]);
  },
};

/* --------------------------------------------------------------- api ------- */
/* The live world: SQLite behind `api/threads.py`, reached through the proxy. */

class ThreadRequestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ThreadRequestError";
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T | null> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: init?.body
      ? { "content-type": "application/json", ...init?.headers }
      : init?.headers,
  });
  // A missing thread is a legitimate answer, not a failure: the sidebar asks
  // for whatever was last open and that thread may have been deleted in
  // another window.
  if (response.status === 404) return null;
  if (response.status === 204) return null;
  if (!response.ok) {
    throw new ThreadRequestError(
      `${init?.method ?? "GET"} ${path} failed with HTTP ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

/** Drop any event the wire sent that `lib/events.ts` does not model.
 *
 *  The same rule `chat-sources.ts` applies to a live stream, applied to a
 *  replayed one: the event model is the contract and storage does not get to
 *  widen it. Without this, a thread stored by a newer backend would render
 *  through a reducer that has never seen those events. */
function acceptEvents(raw: unknown): ChatEvent[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter(isChatEvent);
}

interface WireMessage {
  id: string;
  role: string;
  content: string;
  events: unknown;
  created_at: string;
  parent_id: string | null;
}

interface WireThread {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: WireMessage[];
}

function toThread(wire: WireThread): Thread {
  return {
    id: wire.id,
    title: wire.title,
    created_at: wire.created_at,
    updated_at: wire.updated_at,
    messages: wire.messages.map((message) => ({
      id: message.id,
      role: message.role === "user" ? "user" : "assistant",
      content: message.content,
      events: acceptEvents(message.events),
      created_at: message.created_at,
      parent_id: message.parent_id,
    })),
  };
}

const apiStore: ThreadStore = {
  async list() {
    return (await request<ThreadSummary[]>("")) ?? [];
  },

  async get(id) {
    const wire = await request<WireThread>(`/${encodeURIComponent(id)}`);
    return wire ? toThread(wire) : null;
  },

  async create(title) {
    // The client brings its own id so a new thread opens without waiting for
    // the round trip — the same reason `POST /chat` creates a thread it has
    // not seen (api/chat.py).
    const wire = await request<WireThread>("", {
      method: "POST",
      body: JSON.stringify({
        id: newId("th"),
        title: title === UNTITLED ? null : title,
      }),
    });
    if (!wire) throw new ThreadRequestError("POST /threads returned no thread");
    return toThread(wire);
  },

  async rename(id, title) {
    const trimmed = title.trim();
    // An empty rename is a no-op, not a nameless thread — and the backend
    // would reject it with a 422 anyway.
    if (!trimmed) return summariseWire(await this.get(id));
    const wire = await request<WireThread>(`/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: trimmed.slice(0, 120) }),
    });
    return wire ? summarise(toThread(wire)) : null;
  },

  async remove(id) {
    await request<null>(`/${encodeURIComponent(id)}`, { method: "DELETE" });
    forgetLayout(id);
  },

  async append(threadId) {
    // Nothing to write: `POST /chat` already stored the user message and the
    // assistant's full event stream (api/chat.py `_persist`), including when
    // the turn failed. Re-reading is what keeps the live render and the reload
    // render on the same data — and it is how the server's auto-title reaches
    // the sidebar.
    return apiStore.get(threadId);
  },

  async put() {
    // The demo thread is a canned-mode artefact. Seeding a fixture into the
    // real store would put a conversation nobody had into the graded database.
  },
};

function summariseWire(thread: Thread | null): ThreadSummary | null {
  return thread ? summarise(thread) : null;
}

/* ------------------------------------------------------------- exports ----- */

function store(): ThreadStore {
  return chatSourceKind() === "live" ? apiStore : localStore;
}

export async function listThreads(): Promise<ThreadSummary[]> {
  return store().list();
}

export async function getThread(id: string): Promise<Thread | null> {
  return store().get(id);
}

export async function createThread(title = UNTITLED): Promise<Thread> {
  return store().create(title);
}

export async function renameThread(
  id: string,
  title: string,
): Promise<ThreadSummary | null> {
  return store().rename(id, title);
}

export async function deleteThread(id: string): Promise<void> {
  return store().remove(id);
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
  return store().append(threadId, message);
}

/** Replace a thread wholesale. Used only to seed the demo thread on first run. */
export async function putThread(thread: Thread): Promise<void> {
  return store().put(thread);
}

/* ---------------------------------------------------------------- layout ---- */
/* The chat/source split is stored per thread (canvas `layout-rules` note). It
 * is view state, not thread content, so it stays out of the thread record and
 * out of what the backend stores — in both worlds. */

function forgetLayout(threadId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(LAYOUT_KEY_PREFIX + threadId);
  } catch {
    /* ignore */
  }
}

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

/* ----------------------------------------------------------- last active ---- */
/* Which thread to reopen on load. Also view state, and also per-browser: two
 * windows on the same threads legitimately sit on different ones, so this never
 * belongs in the thread record. Without it a reload jumps to whichever thread
 * happens to be newest, and "reload replays this thread" stops being true. */

export function getActiveThreadId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

export function setActiveThreadId(threadId: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (threadId === null) window.localStorage.removeItem(ACTIVE_KEY);
    else window.localStorage.setItem(ACTIVE_KEY, threadId);
  } catch {
    /* ignore */
  }
}
