"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  CheckIcon,
  DatabaseIcon,
  KeyIcon,
  Loader2Icon,
  PlayIcon,
  RefreshCwIcon,
  RotateCcwIcon,
} from "lucide-react";

import type { SetupStatus } from "@/hooks/use-setup-status";
import { useIngestLog } from "@/hooks/use-ingest-log";
import { Button } from "@/components/ui/button";
import { Cap, Meta } from "@/components/reqtrace/text";
import { ThemeToggle } from "@/components/reqtrace/theme-toggle";

/**
 * The first-run screen (story S5.6.1) — canvas artboard 5, first-run cell.
 *
 * Its whole reason to exist is that **a fresh clone must never show a stack
 * trace**. Every way this application can be unusable arrives here as a
 * sentence and, where possible, a button:
 *
 *   backend not running   -> how to start it, and a retry
 *   no index              -> run ingestion, with live progress
 *   no API key            -> where the key goes, and what still works without
 *   index present, stale  -> restart required, said plainly
 *
 * The canvas's first-run rule is followed exactly: **a disabled control says
 * why it is disabled.** There is no button here that can be pressed to no
 * effect and no explanation offered only on hover.
 *
 * The one thing this screen must not do is claim readiness the process cannot
 * deliver. Ingestion writes the database the running API read at boot, so a
 * finished run reports `restart_required` and this screen says restart rather
 * than dropping the user into a chat backed by an index the server has not
 * loaded.
 */

export function SetupScreen({
  status,
  onRecheck,
  unreachable = false,
}: {
  status: SetupStatus | null;
  onRecheck: () => void;
  unreachable?: boolean;
}) {
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const running = status?.ingest.status === "running";
  const { lines, phase, error: logError } = useIngestLog(running || starting);

  const restartRequired =
    status?.ingest.restart_required === true || phase === "succeeded";

  async function startIngestion() {
    setStarting(true);
    setStartError(null);
    try {
      const response = await fetch("/api/py/setup/ingest", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      onRecheck();
    } catch {
      setStarting(false);
      setStartError(
        "Could not start ingestion — the backend stopped responding. Check " +
          "the terminal running `make dev`.",
      );
    }
  }

  return (
    <main className="flex min-h-dvh flex-col bg-background">
      <header className="flex h-12 flex-none items-center justify-between border-b px-5">
        <div className="flex items-baseline gap-2">
          <span className="text-[13px] font-semibold tracking-[-0.01em]">
            ReqTrace
          </span>
          <Meta>setup</Meta>
        </div>
        <ThemeToggle />
      </header>

      <div className="flex min-h-0 flex-1 items-start justify-center overflow-y-auto px-5 py-10">
        <div className="w-full max-w-[560px]">
          <h1 className="text-[19px] leading-[25px] font-semibold tracking-[-0.015em]">
            {unreachable
              ? "The ReqTrace backend is not running"
              : "Finish setting up ReqTrace"}
          </h1>
          <p className="mt-2 text-[12.5px] leading-[19px] text-muted-foreground">
            {unreachable
              ? "Nothing is wrong with your clone — the API just is not up yet."
              : "The app is running. It needs a corpus to answer from before it can be used."}
          </p>

          {unreachable ? (
            <BackendDown onRecheck={onRecheck} />
          ) : status ? (
            <>
              <Checklist status={status} />
              {status.error ? (
                <Note tone="warn" icon={AlertTriangleIcon}>
                  {status.error}
                </Note>
              ) : null}

              {restartRequired ? (
                <Note tone="ok" icon={RotateCcwIcon}>
                  Ingestion finished. This server is still serving the index it
                  loaded at boot, so <strong>restart it</strong> — stop{" "}
                  <code className="font-mono">make dev</code> and run it again —
                  then reload this page.
                </Note>
              ) : null}

              <Actions
                status={status}
                running={running || starting}
                onStart={startIngestion}
                onRecheck={onRecheck}
                restartRequired={restartRequired}
              />

              {startError ? (
                <Note tone="warn" icon={AlertTriangleIcon}>
                  {startError}
                </Note>
              ) : null}
              {logError ? (
                <Note tone="warn" icon={AlertTriangleIcon}>
                  {logError}
                </Note>
              ) : null}

              {lines.length > 0 ? <IngestLog lines={lines} /> : null}
            </>
          ) : (
            <div className="mt-8 flex items-center gap-2 text-muted-foreground">
              <Loader2Icon className="size-3.5 animate-spin" />
              <span className="text-[12.5px]">Checking…</span>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

/* ------------------------------------------------------------------------- */

function BackendDown({ onRecheck }: { onRecheck: () => void }) {
  return (
    <>
      <div className="mt-6 rounded-lg border bg-card p-4">
        <Cap>Start it</Cap>
        <pre className="mt-2 overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-[11.5px] leading-[18px]">
          make dev
        </pre>
        <p className="mt-2.5 text-[12px] leading-[18px] text-muted-foreground">
          That boots the API on <span className="font-mono">:8000</span> and
          this frontend on <span className="font-mono">:3000</span>. This page
          reaches the API through the Next.js proxy, so there is nothing else to
          configure.
        </p>
      </div>
      <div className="mt-4">
        <Button size="sm" onClick={onRecheck}>
          <RefreshCwIcon className="size-3.5" />
          Check again
        </Button>
      </div>
    </>
  );
}

function Checklist({ status }: { status: SetupStatus }) {
  const items = [
    {
      ok: status.index_present && status.requirements > 0,
      icon: DatabaseIcon,
      label: "Corpus index",
      detail:
        status.requirements > 0
          ? `${status.requirements.toLocaleString()} requirements · ${status.code_units.toLocaleString()} code units · ${status.indexed_chunks.toLocaleString()} indexed chunks`
          : "Not built yet — run ingestion below.",
    },
    {
      ok: status.snapshot_present,
      icon: DatabaseIcon,
      label: "Code snapshot",
      detail: status.snapshot_present
        ? "The pinned repository has been fetched."
        : "Not fetched yet — ingestion fetches it.",
    },
    {
      ok: status.api_key_present,
      icon: KeyIcon,
      label: "OpenRouter API key",
      detail: status.api_key_present
        ? "Present."
        : "Missing. Put OPENROUTER_API_KEY in backend/.env, then restart the API. Without it, ingestion can still build the keyword index (no vectors) but no question can be answered.",
    },
  ];

  return (
    <ul className="mt-6 flex flex-col gap-2">
      {items.map((item) => (
        <li
          key={item.label}
          className="flex items-start gap-3 rounded-lg border bg-card px-3.5 py-3"
        >
          <span
            className={
              item.ok
                ? "mt-0.5 flex size-4 flex-none items-center justify-center rounded-full bg-foreground text-background"
                : "mt-0.5 flex size-4 flex-none items-center justify-center rounded-full border border-dashed text-muted-foreground"
            }
          >
            {item.ok ? <CheckIcon className="size-2.5" /> : null}
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[12.5px] leading-[17px] font-medium">
              {item.label}
            </div>
            <p className="mt-0.5 text-[11.5px] leading-[17px] text-muted-foreground">
              {item.detail}
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function Actions({
  status,
  running,
  onStart,
  onRecheck,
  restartRequired,
}: {
  status: SetupStatus;
  running: boolean;
  onStart: () => void;
  onRecheck: () => void;
  restartRequired: boolean;
}) {
  // The canvas's first-run rule: a disabled control says why, in text, not on
  // hover. So the reason is rendered beside the button rather than as a title.
  const blocked = running
    ? "Ingestion is running."
    : restartRequired
      ? "Restart the API to load what was just built."
      : null;

  return (
    <div className="mt-5 flex flex-wrap items-center gap-3">
      <Button size="sm" onClick={onStart} disabled={running || restartRequired}>
        {running ? (
          <Loader2Icon className="size-3.5 animate-spin" />
        ) : (
          <PlayIcon className="size-3.5" />
        )}
        {status.index_present && status.requirements > 0
          ? "Re-run ingestion"
          : "Run ingestion"}
      </Button>
      <Button size="sm" variant="outline" onClick={onRecheck}>
        <RefreshCwIcon className="size-3.5" />
        Re-check
      </Button>
      {blocked ? (
        <span className="text-[11.5px] text-muted-foreground">{blocked}</span>
      ) : (
        <span className="text-[11.5px] text-muted-foreground">
          Fetches four AUTOSAR PDFs and the pinned repository, then builds the
          index. About a minute.
        </span>
      )}
    </div>
  );
}

function IngestLog({ lines }: { lines: string[] }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [lines.length]);

  return (
    <div className="mt-5">
      <Cap>Ingestion log</Cap>
      <div className="mt-2 max-h-[280px] overflow-y-auto rounded-lg border bg-muted/40 p-3">
        {lines.map((line, index) => (
          <div
            key={index}
            className="font-mono text-[11px] leading-[17px] whitespace-pre-wrap"
          >
            {line || " "}
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function Note({
  tone,
  icon: Icon,
  children,
}: {
  tone: "ok" | "warn";
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}) {
  return (
    <div
      className={
        tone === "warn"
          ? "mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3.5 py-3"
          : "mt-4 flex items-start gap-2.5 rounded-lg border bg-card px-3.5 py-3"
      }
    >
      <Icon className="mt-0.5 size-3.5 flex-none text-muted-foreground" />
      <p className="text-[12px] leading-[18px]">{children}</p>
    </div>
  );
}
