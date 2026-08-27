"use client";

/**
 * Thread state for the shell.
 *
 * Every call here goes through `lib/threads.ts`, and nothing in this file (or in
 * any component) touches storage or `fetch` directly. That is the seam F5.1 is
 * built around: when WP3 lands, `lib/threads.ts` starts calling
 * `/api/py/threads` and this hook does not change, because it is already async
 * and already re-reads what the store returns rather than mutating its own copy.
 *
 * "Threads survive reload" is therefore a property of the store, not of the UI.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  appendMessage,
  createThread,
  deleteThread,
  getActiveThreadId,
  getThread,
  listThreads,
  putThread,
  renameThread,
  setActiveThreadId,
  type NewMessage,
  type Thread,
  type ThreadSummary,
} from "@/lib/threads";
import { chatSourceKind } from "@/lib/chat-sources";
import { buildDemoThread } from "@/lib/fixtures/canned-conversation";

export interface UseThreadsResult {
  summaries: ThreadSummary[];
  thread: Thread | null;
  loading: boolean;
  select: (id: string) => Promise<void>;
  create: () => Promise<Thread>;
  rename: (id: string, title: string) => Promise<void>;
  /** Returns the deleted thread so the caller can offer an undo. */
  remove: (id: string) => Promise<Thread | null>;
  restore: (thread: Thread) => Promise<void>;
  append: (threadId: string, message: NewMessage) => Promise<Thread | null>;
}

export function useThreads(): UseThreadsResult {
  const [summaries, setSummaries] = useState<ThreadSummary[]>([]);
  const [thread, setThread] = useState<Thread | null>(null);
  const [loading, setLoading] = useState(true);
  const booted = useRef(false);

  // Every call below is failure-tolerant, and that is not defensive padding:
  // with a live backend these are network calls, and a thrown one used to take
  // down the whole send. Killing the API mid-session cleared the composer and
  // did nothing else — no message, no error, no toast — because `append` threw
  // before the chat stream ever started. The stream has its own error handling
  // and produces the "could not reach the backend" event the UI already
  // renders; bookkeeping around it must never pre-empt that.
  const refresh = useCallback(async () => {
    try {
      setSummaries(await listThreads());
    } catch {
      // Keep the list we have. Blanking the sidebar because one refresh failed
      // would lose the user's place over a transient error.
    }
  }, []);

  const select = useCallback(async (id: string) => {
    let found: Thread | null = null;
    try {
      found = await getThread(id);
    } catch {
      found = null;
    }
    setThread(found);
    setActiveThreadId(found?.id ?? null);
  }, []);

  // Seeding the canned conversation on an empty store is what makes F5.2's
  // acceptance vehicle visible on first load, and it is stored exactly like a
  // real thread — full event array per message — so reload replays it rather
  // than re-seeding it.
  //
  // Gated on the chat source being canned. The demo thread is a fixture, and
  // once WP3 makes `live` the default an unconditional seed would write a
  // fixture into a real user's thread list on their very first visit.
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;

    void (async () => {
      let existing = await listThreads();
      if (existing.length === 0 && chatSourceKind() === "canned") {
        await putThread(buildDemoThread());
        existing = await listThreads();
      }
      setSummaries(existing);
      // Reopen the thread the user was last on, falling back to the most
      // recently updated one. Without the pointer a reload jumps to whatever is
      // newest, and "reload replays this thread" would not be true.
      const remembered = getActiveThreadId();
      const first =
        existing.find((item) => item.id === remembered) ?? existing[0];
      if (first) {
        setThread(await getThread(first.id));
        setActiveThreadId(first.id);
      }
      setLoading(false);
    })();
  }, []);

  const create = useCallback(async () => {
    const created = await createThread();
    await refresh();
    setThread(created);
    setActiveThreadId(created.id);
    return created;
  }, [refresh]);

  const rename = useCallback(
    async (id: string, title: string) => {
      await renameThread(id, title);
      await refresh();
      setThread((current) =>
        current && current.id === id
          ? { ...current, title: title.trim() || current.title }
          : current,
      );
    },
    [refresh],
  );

  const remove = useCallback(
    async (id: string) => {
      const removed = await getThread(id);
      await deleteThread(id);
      const remaining = await listThreads();
      setSummaries(remaining);
      if (thread?.id === id) {
        const next = remaining[0] ? await getThread(remaining[0].id) : null;
        setThread(next);
        setActiveThreadId(next?.id ?? null);
      }
      return removed;
    },
    [thread],
  );

  const restore = useCallback(
    async (value: Thread) => {
      await putThread(value);
      await refresh();
      setThread(value);
      setActiveThreadId(value.id);
    },
    [refresh],
  );

  // Takes the thread id explicitly rather than reading the active thread: a
  // stream settles asynchronously, and the message has to land on the thread it
  // was sent to even if the user has since selected another one.
  const append = useCallback(
    async (threadId: string, message: NewMessage) => {
      let updated: Thread | null = null;
      try {
        updated = await appendMessage(threadId, message);
      } catch {
        // In live mode this is a re-read: `POST /chat` already persisted the
        // turn. Failing it means the backend went away, which the stream is
        // about to report properly — so leave the transcript alone and let it.
        return null;
      }
      setThread((current) =>
        updated && current?.id === threadId ? updated : current,
      );
      await refresh();
      return updated;
    },
    [refresh],
  );

  return {
    summaries,
    thread,
    loading,
    select,
    create,
    rename,
    remove,
    restore,
    append,
  };
}
