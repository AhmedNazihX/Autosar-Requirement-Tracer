"use client";

import { useState } from "react";
import {
  BracketsIcon,
  MessageSquareIcon,
  PanelLeftIcon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react";

import { CANIF_FIXTURE_SHA } from "@/lib/fixtures/canif-c";
import { relativeTime, pluralise } from "@/lib/format";
import type { ThreadSummary } from "@/lib/threads";
import { cn } from "@/lib/utils";
import type { BackendHealth } from "@/hooks/use-backend-health";
import type { SetupStatus } from "@/hooks/use-setup-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Cap, Meta } from "@/components/reqtrace/text";

/**
 * The thread sidebar — 264 px fixed, or a 44 px icon rail below 1100 px
 * (canvas `layout-rules` note).
 *
 * Every read and write goes through `lib/threads.ts`, which is the seam: today
 * its body is localStorage, when WP3 lands it is `fetch("/api/py/threads")`, and
 * this component does not change either way. It never touches storage itself.
 */
export function ThreadSidebar({
  threads,
  activeId,
  loading,
  collapsed,
  health,
  setup,
  onRecheckHealth,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  onToggleCollapsed,
}: {
  threads: readonly ThreadSummary[];
  activeId: string | null;
  loading: boolean;
  collapsed: boolean;
  health: BackendHealth;
  /** `GET /setup/status`, when there is a backend to have asked. `null` in
   *  canned mode, where the footer falls back to what the fixture knows. */
  setup: SetupStatus | null;
  onRecheckHealth: () => void;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onToggleCollapsed: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);

  if (collapsed) {
    return (
      <div className="flex w-11 flex-none flex-col items-center gap-1 border-r bg-sidebar py-2">
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={onToggleCollapsed}
                aria-label="Show thread list"
              >
                <PanelLeftIcon />
              </Button>
            }
          />
          <TooltipContent side="right">Show thread list</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={onCreate}
                aria-label="New thread"
              >
                <PlusIcon />
              </Button>
            }
          />
          <TooltipContent side="right">New thread</TooltipContent>
        </Tooltip>
        <span aria-hidden className="my-1 h-px w-5 bg-sidebar-border" />
        {threads.slice(0, 8).map((thread) => (
          <Tooltip key={thread.id}>
            <TooltipTrigger
              render={
                <Button
                  variant={thread.id === activeId ? "secondary" : "ghost"}
                  size="icon-sm"
                  onClick={() => onSelect(thread.id)}
                  aria-label={thread.title}
                >
                  <MessageSquareIcon />
                </Button>
              }
            />
            <TooltipContent side="right">{thread.title}</TooltipContent>
          </Tooltip>
        ))}
      </div>
    );
  }

  return (
    <div className="flex w-[264px] flex-none flex-col border-r bg-sidebar">
      <div className="flex h-12 flex-none items-center gap-2 border-b border-sidebar-border pr-2.5 pl-3">
        <span className="flex size-[22px] flex-none items-center justify-center rounded-md bg-primary text-primary-foreground">
          <BracketsIcon className="size-3.5" />
        </span>
        <span className="flex-1 text-sm font-semibold tracking-[-0.01em]">
          ReqTrace
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onToggleCollapsed}
          aria-label="Hide thread list"
        >
          <PanelLeftIcon />
        </Button>
      </div>

      <div className="px-3 pt-2.5 pb-1.5">
        <Button className="w-full" onClick={onCreate}>
          <PlusIcon />
          New thread
        </Button>
      </div>

      <div className="flex items-center gap-1.5 px-3.5 pt-2 pb-1">
        <Cap className="flex-1">Threads</Cap>
        <Meta>{loading ? "" : threads.length}</Meta>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {loading ? (
          <div className="flex flex-col gap-2 px-1 py-1">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-[88%]" />
            <Skeleton className="h-8 w-[72%]" />
          </div>
        ) : threads.length === 0 ? (
          <p className="px-1.5 py-2 text-[11.5px] leading-4 text-muted-foreground">
            No threads yet. Start one and it will appear here — and survive a
            reload.
          </p>
        ) : (
          <div className="flex flex-col gap-px">
            {threads.map((thread) => (
              <ThreadRow
                key={thread.id}
                thread={thread}
                active={thread.id === activeId}
                editing={editingId === thread.id}
                onSelect={() => onSelect(thread.id)}
                onStartEdit={() => setEditingId(thread.id)}
                onCancelEdit={() => setEditingId(null)}
                onCommitEdit={(title) => {
                  setEditingId(null);
                  onRename(thread.id, title);
                }}
                onDelete={() => onDelete(thread.id)}
              />
            ))}
          </div>
        )}
      </div>

      <div className="h-px flex-none bg-sidebar-border" />

      <div className="flex flex-none flex-col gap-1 px-3 pt-2.5 pb-3">
        <div className="flex items-center gap-1.5">
          <span
            aria-hidden
            className={cn(
              "size-1.5 flex-none rounded-full",
              health.status === "ok"
                ? "bg-verdict-implemented"
                : health.status === "checking"
                  ? "bg-verdict-missing"
                  : "bg-destructive",
            )}
          />
          <span className="flex-1 text-xs leading-4">
            {health.status === "ok"
              ? `Backend ready · v${health.version}`
              : health.status === "checking"
                ? "Checking backend…"
                : "Backend not reachable"}
          </span>
          <span className="flex h-[18px] flex-none items-center rounded-md border bg-muted px-1.5 font-mono text-[10px] leading-none text-muted-foreground">
            {(setup?.git_sha ?? CANIF_FIXTURE_SHA).slice(0, 7)}
          </span>
        </div>
        <Meta>
          {health.status !== "ok"
            ? "Start it with `make dev` — the UI keeps working"
            : setup
              ? `${setup.requirements.toLocaleString()} requirements · ${setup.code_units.toLocaleString()} code units · ${setup.indexed_chunks.toLocaleString()} indexed`
              : "Index status unavailable"}
        </Meta>
        {/*
          Where this SHA comes from, said accurately in both worlds. In live
          mode it is the manifest's pinned snapshot, reported by
          `GET /setup/status` — which is the SHA every citation and every
          verdict is only true against. In canned mode there is no backend to
          ask, so it falls back to the committed fixture's SHA and says so:
          the two agree today only because the fixture was cut from the pinned
          snapshot, and claiming otherwise would become wrong the first time
          either moves.
        */}
        <Meta>
          {setup?.git_sha
            ? `Pinned snapshot · ${setup.corpus_version ?? ""}`.trim()
            : "Snapshot sha of the committed code fixture"}
        </Meta>
        {health.status === "unreachable" ? (
          <Button
            variant="ghost"
            size="xs"
            className="-ml-2 w-fit"
            onClick={onRecheckHealth}
          >
            <RefreshCwIcon />
            Re-check
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function ThreadRow({
  thread,
  active,
  editing,
  onSelect,
  onStartEdit,
  onCancelEdit,
  onCommitEdit,
  onDelete,
}: {
  thread: ThreadSummary;
  active: boolean;
  editing: boolean;
  onSelect: () => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onCommitEdit: (title: string) => void;
  onDelete: () => void;
}) {
  if (editing) {
    // Uncontrolled on purpose: the input's value only matters at commit time, so
    // there is no draft state to keep in step with the thread it came from.
    return (
      <div className="px-1 py-0.5">
        <Input
          autoFocus
          defaultValue={thread.title}
          onFocus={(event) => event.currentTarget.select()}
          onBlur={(event) => onCommitEdit(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              onCommitEdit(event.currentTarget.value);
            }
            if (event.key === "Escape") {
              event.preventDefault();
              onCancelEdit();
            }
          }}
          className="h-7 text-[12.8px]"
          aria-label="Thread title"
        />
      </div>
    );
  }

  return (
    <div
      className={cn(
        "group flex items-start gap-1.5 rounded-lg px-2 py-1.5",
        active ? "bg-sidebar-accent" : "hover:bg-sidebar-accent",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        onDoubleClick={onStartEdit}
        className="min-w-0 flex-1 text-left"
      >
        <span
          className={cn(
            "block truncate text-[12.8px] leading-[17px]",
            active ? "font-medium" : "font-normal",
          )}
        >
          {thread.title}
        </span>
        <span className="mt-px block truncate font-mono text-[10.5px] leading-[14px] text-muted-foreground">
          {relativeTime(thread.updated_at)} ·{" "}
          {pluralise(thread.message_count, "message")}
        </span>
      </button>
      <span className="flex flex-none items-center gap-px opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={onStartEdit}
          aria-label={`Rename ${thread.title}`}
        >
          <PencilIcon />
        </Button>
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={onDelete}
          aria-label={`Delete ${thread.title}`}
        >
          <Trash2Icon />
        </Button>
      </span>
    </div>
  );
}
