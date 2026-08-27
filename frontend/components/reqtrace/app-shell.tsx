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
import { toExchanges } from "@/lib/exchanges";
import {
  targetForCitation,
  type SourceTab,
  type SourceTarget,
} from "@/lib/source-target";
import { getPaneLayout, setPaneLayout } from "@/lib/threads";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useBackendHealth } from "@/hooks/use-backend-health";
import type { SetupStatus } from "@/hooks/use-setup-status";
import { useThreads } from "@/hooks/use-threads";
import { useViewport } from "@/hooks/use-viewport";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { CheckpointRail } from "./checkpoint-rail";
import { ChatPane } from "./chat/chat-pane";
import { SourcePane } from "./source/source-pane";
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
  } | null>(null);

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

      void threads.append(target, { role: "assistant", content: text, events });

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
    events: liveEvents,
    isStreaming,
    send,
    stop,
  } = useChatStream({ source, onSettled });

  const exchanges = useMemo(() => toExchanges(thread?.messages ?? []), [thread]);
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
  const sourceTarget = activePin ? activePin.target : latestTarget;
  const sourceTab: SourceTab = activePin
    ? activePin.tab
    : (latestTarget?.tab ?? "document");

  // The split is view state, per thread, so it lives outside the thread record
  // and outside whatever the backend will eventually store.
  const layout = useMemo(
    () => (threadId ? (getPaneLayout(threadId) ?? undefined) : undefined),
    [threadId],
  );

  const openCitation = useCallback(
    (citation: RequirementCitation | CodeCitation) => {
      const target = targetForCitation(citation);
      if (!target) return;
      setPinned({ key: transcriptKey, target, tab: target.tab });
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
      await threads.append(active.id, {
        role: "user",
        content: text,
        events: [],
      });
      send(active.id, text);
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

  const sourcePane = (
    <SourcePane
      target={sourceTarget}
      tab={sourceTab}
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

          <DisabledWithReason reason="The report drawer is story F5.5 — POST /reports does not exist on this backend yet.">
            <Button variant="outline" size="sm" disabled>
              <Table2Icon />
              Generate report
            </Button>
          </DisabledWithReason>

          <DisabledWithReason reason="Thread export is story F5.7.">
            <Button
              variant="ghost"
              size="icon-sm"
              disabled
              aria-label="Export thread"
            >
              <DownloadIcon />
            </Button>
          </DisabledWithReason>

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
              onSelect={(exchange) => scrollToExchange(exchange.id)}
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
          className="flex w-full flex-col gap-0 p-0 sm:max-w-[640px]"
        >
          <SheetTitle className="sr-only">Source</SheetTitle>
          {sourcePane}
        </SheetContent>
      </Sheet>
    </div>
  );
}

/**
 * A control that is deliberately not wired yet, with the reason attached.
 *
 * The canvas's first-run cell states the rule: a disabled control says why it is
 * disabled. A disabled button swallows pointer events, so the tooltip has to be
 * anchored to a wrapper rather than to the button.
 */
function DisabledWithReason({
  reason,
  children,
}: {
  reason: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger render={<span tabIndex={0} className="inline-flex" />}>
        {children}
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-[260px]">
        {reason}
      </TooltipContent>
    </Tooltip>
  );
}
