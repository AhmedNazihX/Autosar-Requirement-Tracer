"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DownloadIcon,
  PanelRightIcon,
  PencilIcon,
  Table2Icon,
} from "lucide-react";
import { toast } from "sonner";

import type { HighlightedFile } from "@/lib/code-highlight";
import { pickChatSource } from "@/lib/chat-sources";
import type { ChatEvent, CodeCitation, RequirementCitation } from "@/lib/events";
import { toExchanges, type Exchange } from "@/lib/exchanges";

import { fetchRequirementCitation } from "@/lib/requirements";
import {
  targetForCitation,
  type SourceTab,
  type SourceTarget,
} from "@/lib/source-target";
import {
  getPaneLayout,
  setPaneLayout,
  type StoredMessage,
} from "@/lib/threads";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useSourceCompanion } from "@/hooks/use-source-companion";
import { useBackendHealth } from "@/hooks/use-backend-health";
import type { SetupStatus } from "@/hooks/use-setup-status";
import { useReport } from "@/hooks/use-report";
import { useThreads } from "@/hooks/use-threads";
import { useViewport } from "@/hooks/use-viewport";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

import { CheckpointRail } from "./checkpoint-rail";
import { ReportDrawer, useReportView } from "./report/report-drawer";
import { ChatPane } from "./chat/chat-pane";
import { SourcePane } from "./source/source-pane";

/** Stable identity, so a thread with no live events does not churn the memos
 *  that depend on `liveEvents` on every render. */
const EMPTY_EVENTS: readonly ChatEvent[] = [];
import { ThemeToggle } from "./theme-toggle";
import { ThreadSidebar } from "./thread-sidebar";

/**
 * The three-zone shell (spec §7), laid out as the canvas draws it:
 *
 *   sidebar 264 (fixed) · checkpoint rail 44 (fixed) · chat 520 · source 612
 *
 * Only the chat/source boundary is draggable, minimum 380 px each, and the
 * position is stored per thread. The source pane keeps the larger default share
 * because it renders an A4 spec page: at 612 px the page sits at ~77% and 10 pt
 * body text lands at ~9.5 px, which is the floor for readable. Give the chat the
 * bigger half and the requirement text stops being legible, which defeats the
 * pane — the default proportion is a readability constraint, not a taste.
 *
 * Collapse order as the window narrows (canvas `layout-rules` note):
 *   < 1280  the source pane becomes an overlay Sheet, opened by a citation click
 *   < 1100  the thread sidebar collapses to a 44 px icon rail; the checkpoint
 *           rail stays, being the cheaper of the two to keep and the harder to
 *           re-find
 *   < 900   the checkpoint rail is hidden (its popover form belongs with F5.4)
 *
 * Above 1920 the app centres rather than stretching: a 1000 px chat column reads
 * badly.
 *
 * All chat state is plain React state plus one SSE hook (spec §7, locked). There
 * is no store and no context — the only reducer is the pure one that turns a
 * message's event array into its render state.
 */
/** The scope the drawer opens on. `CanIf` is the only module with a real
 *  implementation in the permitted snapshot (finding A1), so it is the one
 *  that produces a matrix with every verdict class in it. */
const DEFAULT_REPORT_MODULE = "CanIf";

export function AppShell({
  codeFile,
  setup = null,
}: {
  codeFile: HighlightedFile;
  /** The readiness snapshot `BootGate` already fetched. Passed down rather
   *  than re-fetched: two components asking the same question can disagree,
   *  and the sidebar footer states index counts a user will believe. */
  setup?: SetupStatus | null;
}) {
  const viewport = useViewport();
  const { health, recheck } = useBackendHealth();
  const threads = useThreads();
  const { thread } = threads;
  const threadId = thread?.id ?? null;

  const [reportOpen, setReportOpen] = useState(false);
  // Owned here, not in the drawer: the drawer closes whenever the user follows
  // a row into the source pane, and Base UI unmounts a closed popup — so a
  // hook inside it would throw the matrix away on the click meant to show it.
  const report = useReport();
  // The reader's place in the matrix — filters, scroll offset, last row
  // opened. Lives here so following an evidence span into the code does not
  // cost it; see `ReportView`.
  const reportView = useReportView();
  const [sidebarHidden, setSidebarHidden] = useState(false);
  const [sourceOpen, setSourceOpen] = useState(true);
  const [renamingTitle, setRenamingTitle] = useState<string | null>(null);
  const [flashExchangeId, setFlashExchangeId] = useState<string | null>(null);
  /**
   * What the user pointed the source pane at, and when. The `key` is the
   * transcript's identity at click time, so a new answer automatically drops the
   * pin and the pane follows the newest citation again — no effect, no reset
   * logic, and no way for the two to disagree.
   */
  const [pinned, setPinned] = useState<{
    key: string;
    target: SourceTarget | null;
    tab: SourceTab;
    /** Set only by a checkpoint restore: the turn the pane was rewound to, so
     *  the pane can say so and offer a way back (story S5.4.1). */
    fromTurn?: number;
  } | null>(null);

  /**
   * The turn that could not be stored.
   *
   * The transcript renders from the thread store, which was local until WP5
   * made it the backend. That change introduced a failure the local store
   * could not have: with the API down, persisting the user message fails, the
   * transcript never shows it, and the whole send vanishes — composer cleared,
   * nothing else. Measured, not theorised: killing the backend mid-session
   * produced exactly that.
   *
   * So a turn that cannot be persisted is held here and rendered anyway. What
   * the user typed stays on screen and the stream's `error` event still gets
   * somewhere to appear, which is what makes "nothing was lost" true rather
   * than merely reassuring. It clears the moment a write succeeds — the store
   * is the source of truth again as soon as there is one.
   */
  const [unsaved, setUnsaved] = useState<{
    threadId: string | null;
    messages: StoredMessage[];
  }>({ threadId: null, messages: [] });

  const transcriptRef = useRef<HTMLDivElement>(null);
  const streamThreadRef = useRef<string | null>(null);
  const lastPromptRef = useRef<string>("");

  // One source, created once. `cannedSource` keeps a cursor across sends, so
  // re-creating it every render would replay the first exchange forever.
  const source = useMemo(() => pickChatSource(), []);

  const onSettled = useCallback(
    (events: ChatEvent[]) => {
      const target = streamThreadRef.current;
      streamThreadRef.current = null;
      if (!target || events.length === 0) return;

      const text = events
        .filter((event) => event.type === "token")
        .map((event) => event.data.text)
        .join("");

      void threads
        .append(target, { role: "assistant", content: text, events })
        .then((stored) => {
          if (stored) setUnsaved({ threadId: null, messages: [] });
          else
            setUnsaved((current) => ({
              threadId: target,
              messages: [
                ...(current.threadId === target ? current.messages : []),
                localMessage("assistant", text, events),
              ],
            }));
        });

      // An `error` event renders inline AND as a toast. The toast is what makes
      // an unreachable backend impossible to mistake for a hang; the inline
      // notice is what still explains it once the toast is gone.
      const failure = events.find((event) => event.type === "error");
      if (failure) {
        toast.error("The answer stopped mid-stream", {
          description: failure.data.message,
        });
      }
    },
    [threads],
  );

  const {
    events: streamEvents,
    threadId: streamThreadId,
    isStreaming: streamIsStreaming,
    send,
    stop,
  } = useChatStream({ source, onSettled });

  /**
   * The in-flight message, but only when it belongs to the thread on screen.
   *
   * `useChatStream` clears its events on the *next* send rather than when a
   * stream ends, so after any finished turn they still describe the thread
   * they arrived for. Rendering them against a new thread showed another
   * conversation's answer; worse, it made the transcript count as non-empty,
   * so `ChatPane` suppressed the empty-thread prompts and drew nothing in
   * their place — a blank pane on every new thread after the first.
   *
   * Derived rather than reset in an effect, which is what
   * `frontend/AGENTS.md` requires. The stream itself is deliberately left
   * running: it persists against its own thread id, so switching away no
   * longer loses the answer, it just stops showing it here.
   */
  const isThisThread = streamThreadId === threadId;
  const liveEvents = isThisThread ? streamEvents : EMPTY_EVENTS;
  const isStreaming = streamIsStreaming && isThisThread;

  const exchanges = useMemo(() => {
    const stored = thread?.messages ?? [];
    // Drop any in-memory message the store has since caught up on. The local
    // store writes *and* returns the message, so between its `setThread` and
    // the `setUnsaved([])` that follows there is a frame where both hold it —
    // and a duplicated question is a visible glitch. Matching on role+content
    // against the tail is enough: the only messages in `unsaved` are the ones
    // from the turn in flight.
    const tail = stored.slice(-2);
    // Held messages belong to the thread they were typed in. Without this
    // check, switching threads mid-turn carried the other conversation's
    // question across — and made the new thread non-empty, which suppressed
    // the empty-thread prompts.
    const held = unsaved.threadId === threadId ? unsaved.messages : [];
    const pending = held.filter(
      (one) =>
        !tail.some(
          (other) => other.role === one.role && other.content === one.content,
        ),
    );
    return toExchanges([...stored, ...pending]);
  }, [thread, threadId, unsaved]);
  const lastExchangeId = exchanges.at(-1)?.id ?? null;

  /* --------------------------------------------------------- source pane --- */

  /**
   * The pane's default view is the newest exchange that had a source. One rule
   * covers both cases: a freshly loaded thread lands on the view it ended on
   * (from stored events alone — spec §11), and a finished answer brings its own
   * citation forward.
   */
  const latestTarget = useMemo(
    () =>
      [...exchanges].reverse().find((exchange) => exchange.target)?.target ??
      null,
    [exchanges],
  );
  const transcriptKey = `${threadId ?? ""}:${exchanges.length}:${
    exchanges.at(-1)?.assistant?.id ?? ""
  }`;
  const activePin = pinned?.key === transcriptKey ? pinned : null;
  const pinnedTarget = activePin ? activePin.target : latestTarget;
  /**
   * The other tab's half, resolved for the target on screen — clicked or not.
   *
   * This used to happen in `openCitation`, which meant only a citation the
   * user *clicked* filled both tabs. A `search_code` turn cites no
   * requirement, so its pane opened on the Code tab with an empty Document
   * tab until someone thought to click a chip. Deriving it here covers the
   * automatic path too, and leaves one place that knows how the two halves
   * are linked.
   */
  const companion = useSourceCompanion(pinnedTarget);
  const sourceTarget = useMemo(
    () =>
      pinnedTarget && !pinnedTarget.companion && companion
        ? { ...pinnedTarget, companion }
        : pinnedTarget,
    [pinnedTarget, companion],
  );
  const sourceTab: SourceTab = activePin
    ? activePin.tab
    : (latestTarget?.tab ?? "document");

  // The split is view state, per thread, so it lives outside the thread record
  // and outside whatever the backend will eventually store.
  const layout = useMemo(
    () => (threadId ? (getPaneLayout(threadId) ?? undefined) : undefined),
    [threadId],
  );

  /**
   * Open a citation — and, for a requirement, the code tied to it.
   *
   * Clicking a requirement used to open its page and leave the code pane
   * empty, so the reader had to work out for themselves which part of the
   * snapshot the requirement corresponded to. Connecting the two is the one
   * job this product exists to do for them.
   *
   * The pane opens immediately on the document and the link is filled in when
   * it resolves: the page is the thing the user asked for, and it must not
   * wait on a second request. The lookup is free — annotations and named
   * symbols are SQL, and a verdict is returned only if already cached — so
   * this cannot start a judge call however often a citation is clicked.
   */
  const openCitation = useCallback(
    (citation: RequirementCitation | CodeCitation) => {
      const target = targetForCitation(citation);
      if (!target) return;
      setPinned({ key: transcriptKey, target, tab: target.tab });
      setSourceOpen(true);
      // The other tab fills itself: `useSourceCompanion` runs off whatever the
      // pane is pointed at, so a click needs to say *what* is being opened and
      // nothing more. It used to do the lookup here, which is why only clicked
      // citations ever filled both tabs.
    },
    [transcriptKey],
  );

  /**
   * A report row points at two things at once — the requirement and the code
   * its verdict cited — so it pins both and lands on the document tab. Spec §7:
   * "row click opens both tabs".
   *
   * The requirement citation is fetched rather than built from the row: a row
   * carries the id, document and page, but the bbox and page count live in
   * storage. Guessing them would put the highlight in the wrong place, which
   * is worse than not drawing one.
   */
  const openReportRow = useCallback(
    async (reqId: string, evidence: CodeCitation | null, tab: SourceTab) => {
      // Close first. The drawer sits over the source pane, so opening a
      // citation behind it would put the thing the user asked to see in the
      // one place they cannot look. The run survives — it lives above.
      setReportOpen(false);
      const citation = await fetchRequirementCitation(reqId);
      if (!citation) return;
      setPinned({
        key: transcriptKey,
        target: { tab, citation, companion: evidence },
        tab,
      });
      setSourceOpen(true);
    },
    [transcriptKey],
  );

  const changeSourceTab = useCallback(
    (tab: SourceTab) => {
      setPinned({ key: transcriptKey, target: sourceTarget, tab });
    },
    [transcriptKey, sourceTarget],
  );

  /* --------------------------------------------------------------- chat ---- */

  const handleSend = useCallback(
    async (text: string) => {
      const active = thread ?? (await threads.create());
      lastPromptRef.current = text;
      streamThreadRef.current = active.id;
      // Paint the message and open the stream *before* touching the store.
      //
      // The store write is bookkeeping; blocking on it delayed the user's own
      // message by ~500 ms of round trip on every send, measured. In live mode
      // it is not even a write — `POST /chat` is what persists a turn — so the
      // await bought nothing at all and cost half a second of the thing the
      // user is waiting to see.
      setUnsaved({
        threadId: active.id,
        messages: [localMessage("user", text, [])],
      });
      send(active.id, text);

      // Local mode really does write here, so drop the in-memory copy once the
      // store holds the same message — otherwise it would render twice.
      void threads
        .append(active.id, { role: "user", content: text, events: [] })
        .then((stored) => {
          const last = stored?.messages.at(-1);
          if (last?.role === "user" && last.content === text)
            setUnsaved({ threadId: null, messages: [] });
        });
    },
    [thread, threads, send],
  );

  const handleRetry = useCallback(() => {
    if (lastPromptRef.current) void handleSend(lastPromptRef.current);
  }, [handleSend]);

  /* --------------------------------------------------------- checkpoints -- */

  const scrollToExchange = useCallback((id: string) => {
    const found = transcriptRef.current?.querySelector<HTMLElement>(
      `[data-exchange-id="${id}"]`,
    );
    if (!found) return;
    found.scrollIntoView({ behavior: "smooth", block: "start" });
    setFlashExchangeId(id);
  }, []);

  /**
   * A checkpoint restore (story S5.4.1): scroll to the turn **and** put the
   * source pane back to what that turn showed.
   *
   * The pane state is `Exchange.target`, which `lib/exchanges.ts` derives from
   * the turn's stored `citation` events — so this is a replay of what was
   * recorded, not a re-derivation from the current conversation. Spec §11 asks
   * for exactly that, and it is why the rail can rewind a thread loaded from
   * SQLite as faithfully as one that just streamed.
   *
   * A turn that cited nothing restores to an empty pane rather than leaving
   * the previous turn's page up, which would attribute a source to a turn that
   * had none.
   */
  const restoreCheckpoint = useCallback(
    (exchange: Exchange) => {
      scrollToExchange(exchange.id);
      setPinned({
        key: transcriptKey,
        target: exchange.target,
        tab: exchange.target?.tab ?? "document",
        fromTurn: exchange.turn,
      });
      setSourceOpen(true);
    },
    [scrollToExchange, transcriptKey],
  );

  const returnToLatest = useCallback(() => setPinned(null), []);

  useEffect(() => {
    if (!flashExchangeId) return;
    const timer = setTimeout(() => setFlashExchangeId(null), 1200);
    return () => clearTimeout(timer);
  }, [flashExchangeId]);

  /* ------------------------------------------------------------- threads -- */

  const handleDelete = useCallback(
    async (id: string) => {
      const removed = await threads.remove(id);
      if (!removed) return;
      // Delete is immediate but reversible. A confirmation dialog is not on the
      // canvas, and thread export (F5.7) does not exist yet, so an undo is the
      // smaller addition that still stops one misclick losing a conversation.
      toast("Thread deleted", {
        description: removed.title,
        action: {
          label: "Undo",
          onClick: () => void threads.restore(removed),
        },
      });
    },
    [threads],
  );

  const commitHeaderRename = useCallback(() => {
    if (renamingTitle === null || !threadId) return;
    void threads.rename(threadId, renamingTitle);
    setRenamingTitle(null);
  }, [renamingTitle, threadId, threads]);

  /* -------------------------------------------------------------- render -- */

  const sidebarCollapsed = viewport.sidebarIsRail || sidebarHidden;
  const sourceIsOverlay = viewport.sourceIsOverlay;
  const showRail = !viewport.railIsPopover;

  const chatPane = (
    <ChatPane
      exchanges={exchanges}
      liveEvents={liveEvents}
      isStreaming={isStreaming}
      transcriptRef={transcriptRef}
      flashExchangeId={flashExchangeId}
      onSend={(text) => void handleSend(text)}
      onStop={stop}
      onOpenCitation={openCitation}
      onRetry={handleRetry}
    />
  );

  const reportDrawer = (
    <ReportDrawer
      open={reportOpen}
      onOpenChange={setReportOpen}
      defaultModule={DEFAULT_REPORT_MODULE}
      report={report}
      view={reportView}
      onOpenRow={(reqId, evidence, tab) =>
        void openReportRow(reqId, evidence, tab)
      }
    />
  );

  const sourcePane = (
    <SourcePane
      target={sourceTarget}
      restoredFrom={
        pinned?.fromTurn != null && pinned.fromTurn < exchanges.length
          ? { turn: pinned.fromTurn, onReturn: returnToLatest }
          : null
      }
      tab={sourceTab}
      onOpenRequirement={openCitation}
      onTabChange={changeSourceTab}
      onClose={() => setSourceOpen(false)}
      codeFile={codeFile}
    />
  );

  return (
    <div className="mx-auto flex h-dvh w-full max-w-[1920px] overflow-hidden">
      <ThreadSidebar
        threads={threads.summaries}
        activeId={threadId}
        loading={threads.loading}
        collapsed={sidebarCollapsed}
        health={health}
        setup={setup}
        onRecheckHealth={recheck}
        onSelect={(id) => void threads.select(id)}
        onCreate={() => void threads.create()}
        onRename={(id, title) => void threads.rename(id, title)}
        onDelete={(id) => void handleDelete(id)}
        onToggleCollapsed={() => setSidebarHidden((value) => !value)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-2 border-b pr-2.5 pl-3.5">
          {renamingTitle !== null && thread ? (
            <Input
              value={renamingTitle}
              onChange={(event) => setRenamingTitle(event.target.value)}
              onBlur={commitHeaderRename}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  commitHeaderRename();
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setRenamingTitle(null);
                }
              }}
              className="h-7 max-w-[380px] text-sm"
              aria-label="Thread title"
            />
          ) : (
            <>
              <h1 className="truncate text-sm font-medium tracking-[-0.005em]">
                {thread?.title ?? "ReqTrace"}
              </h1>
              {thread ? (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => setRenamingTitle(thread.title)}
                  aria-label="Rename thread"
                >
                  <PencilIcon />
                </Button>
              ) : null}
            </>
          )}

          <span className="flex-1" />

          <Button
            variant="outline"
            size="sm"
            onClick={() => setReportOpen(true)}
          >
            <Table2Icon />
            Generate report
          </Button>

          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Export thread"
                  disabled={!threadId}
                >
                  <DownloadIcon />
                </Button>
              }
            />
            <DropdownMenuContent align="end">
              {/* Two formats, and the labels say what each is *for*. Markdown
                  is the one a person reads; JSON is the raw event stream,
                  which is what makes an exported thread replayable rather
                  than merely readable (spec §11). */}
              <DropdownMenuItem
                render={
                  <a
                    href={`/api/py/threads/${threadId}/export?fmt=md`}
                    download
                  >
                    Markdown — readable, citations linked
                  </a>
                }
              />
              <DropdownMenuItem
                render={
                  <a
                    href={`/api/py/threads/${threadId}/export?fmt=json`}
                    download
                  >
                    JSON — the raw event stream
                  </a>
                }
              />
            </DropdownMenuContent>
          </DropdownMenu>

          <ThemeToggle />
          <span aria-hidden className="mx-0.5 h-[18px] w-px bg-border" />
          <Button
            variant={sourceOpen ? "secondary" : "ghost"}
            size="icon-sm"
            onClick={() => setSourceOpen((value) => !value)}
            aria-label={sourceOpen ? "Hide source pane" : "Show source pane"}
          >
            <PanelRightIcon />
          </Button>
        </header>

        <div className="flex min-h-0 flex-1">
          {showRail ? (
            <CheckpointRail
              exchanges={exchanges}
              activeExchangeId={lastExchangeId}
              streamingExchangeId={isStreaming ? lastExchangeId : null}
              onSelect={restoreCheckpoint}
            />
          ) : null}

          {sourceOpen && !sourceIsOverlay ? (
            <ResizablePanelGroup
              key={threadId ?? "none"}
              orientation="horizontal"
              className="min-h-0 flex-1"
              defaultLayout={layout}
              onLayoutChanged={(next) => {
                if (threadId) setPaneLayout(threadId, next);
              }}
            >
              <ResizablePanel
                id="chat"
                defaultSize={520}
                minSize={380}
                className="flex min-h-0 flex-col border-r"
              >
                {chatPane}
              </ResizablePanel>
              <ResizableHandle />
              <ResizablePanel
                id="source"
                minSize={380}
                className="flex min-h-0 flex-col"
              >
                {sourcePane}
              </ResizablePanel>
            </ResizablePanelGroup>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">{chatPane}</div>
          )}
        </div>
      </div>

      {/* Below 1280 the source pane is an overlay, opened by a citation click
          and dismissed with Escape. */}
      <Sheet
        open={sourceOpen && sourceIsOverlay}
        onOpenChange={setSourceOpen}
      >
        <SheetContent
          side="right"
          showCloseButton={false}
          // Same specificity rule as the report drawer: without the
          // `data-[side=right]:` qualifier this loses to the base
          // `max-w-sm` and the overlay pane comes out 384 px wide.
          className="flex w-full flex-col gap-0 p-0 data-[side=right]:sm:max-w-[640px]"
        >
          <SheetTitle className="sr-only">Source</SheetTitle>
          {sourcePane}
        </SheetContent>
      </Sheet>

      {reportDrawer}
    </div>
  );
}

/**
 * A message that exists only in this tab, for a turn the backend could not be
 * told about. Shaped exactly like a stored one so `toExchanges` and every
 * renderer treat it identically — the transcript should not have two kinds of
 * message in it.
 */
function localMessage(
  role: "user" | "assistant",
  content: string,
  events: ChatEvent[],
): StoredMessage {
  return {
    id: `unsaved_${role}_${content.length}_${events.length}`,
    role,
    content,
    events,
    created_at: new Date().toISOString(),
    parent_id: null,
  };
}
