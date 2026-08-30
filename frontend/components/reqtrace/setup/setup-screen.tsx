"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  ArrowRightIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CircleAlertIcon,
  Loader2Icon,
  PlayIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  ServerIcon,
  XIcon,
} from "lucide-react";

import type { IngestStep, SetupStatus } from "@/hooks/use-setup-status";
import { useIngestLog } from "@/hooks/use-ingest-log";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Cap, Meta } from "@/components/reqtrace/text";
import { ThemeToggle } from "@/components/reqtrace/theme-toggle";

/**
 * The first-run screen (story S5.6.1) — canvas artboards "First run — nothing
 * indexed" and "Ingestion, with progress".
 *
 * Its whole reason to exist is that **a fresh clone must never show a stack
 * trace**. Every way this application can be unusable arrives here as a
 * sentence and, where possible, a button:
 *
 *   backend not running   -> how to start it, and a retry
 *   no API key            -> the exact line to paste, and a re-check
 *   no index              -> run ingestion, with the five-step tracker
 *   ingestion finished    -> "Open ReqTrace", which reloads the server's
 *                            state in place (POST /setup/reload) — restart
 *                            instructions only as the fallback if that fails
 *
 * The canvas's first-run rule is followed exactly: **a disabled control says
 * why it is disabled** — the reason renders beside the button, never on hover.
 * And per the canvas, Run ingestion stays disabled until a key is set: the
 * backend can build a keyword-only index without one, but offering that as
 * the default first-run path would hand a fresh user a half-working app.
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
  const [requested, setRequested] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const jobStatus = status?.ingest.status ?? "idle";
  const running = jobStatus === "running";
  // "Starting" is DERIVED, not latched: it covers only the gap between the
  // POST and the next status read that shows the job. A latched flag shipped
  // once and stuck true forever after a successful launch — an instantly
  // failing run then kept showing Cancel (a no-op on a dead job) and never
  // offered Retry.
  const starting = requested && jobStatus === "idle";
  const { lines, steps, phase, error: logError, reset } = useIngestLog(
    running || starting,
  );

  const restartRequired =
    status?.ingest.restart_required === true || phase === "succeeded";

  // The stream's snapshot wins while it is delivering; the status poll fills
  // in for a page that loads mid-run before the stream has replayed.
  const stepList = steps ?? status?.ingest.steps ?? [];
  const showTracker = running || starting || jobStatus !== "idle";

  async function startIngestion() {
    setRequested(true);
    setStartError(null);
    // A retry must not inherit the previous run's terminal state — a stale
    // "failed" phase would caption a fresh run "Ingestion failed".
    reset();
    try {
      const response = await fetch("/api/py/setup/ingest", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      onRecheck();
    } catch {
      setRequested(false);
      setStartError(
        "Could not start ingestion — the backend stopped responding. Check " +
          "the terminal running `make dev`.",
      );
    }
  }

  async function cancelIngestion() {
    try {
      await fetch("/api/py/setup/ingest/cancel", { method: "POST" });
    } catch {
      // The run keeps going; the stream is still the source of truth.
    }
    onRecheck();
  }

  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);

  /** The "Open ReqTrace" button: ask the backend to reload its state from
   *  disk, then re-check — the boot gate renders the app the moment status
   *  reports ready. No server restart, no page reload. */
  async function openApp() {
    setOpening(true);
    setOpenError(null);
    try {
      const response = await fetch("/api/py/setup/reload", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const fresh: unknown = await response.json();
      const ready =
        typeof fresh === "object" &&
        fresh !== null &&
        (fresh as { ready?: unknown }).ready === true;
      if (!ready) {
        const reason =
          typeof fresh === "object" && fresh !== null
            ? String((fresh as { error?: unknown }).error ?? "")
            : "";
        throw new Error(reason || "the new index did not load");
      }
      onRecheck();
    } catch (cause) {
      setOpening(false);
      setOpenError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <main className="flex min-h-dvh flex-col bg-background">
      <header className="flex h-12 flex-none items-center justify-between border-b px-5">
        <div className="flex items-baseline gap-2">
          <span className="text-[13px] font-semibold tracking-[-0.01em]">
            ReqTrace
          </span>
          <Meta>{showTracker ? "ingesting" : "setup"}</Meta>
        </div>
        <ThemeToggle />
      </header>

      <div className="flex min-h-0 flex-1 items-start justify-center overflow-y-auto px-5 py-10">
        <div className="w-full max-w-[560px]">
          {unreachable ? (
            <>
              <Heading
                title="The ReqTrace backend is not running"
                sub="Nothing is wrong with your clone — the API just is not up yet."
              />
              <BackendDown onRecheck={onRecheck} />
            </>
          ) : status ? (
            showTracker ? (
              <Tracker
                status={status}
                steps={stepList}
                lines={lines}
                phase={phase}
                running={running || starting}
                restartRequired={restartRequired}
                logError={logError}
                opening={opening}
                openError={openError}
                onOpen={openApp}
                onCancel={cancelIngestion}
                onRetry={startIngestion}
                onRecheck={onRecheck}
              />
            ) : (
              <>
                <Heading
                  title="Set up ReqTrace"
                  sub={`${describeCorpus(status)} have to be on disk before ReqTrace can answer anything. Nothing is fetched until you say so.`}
                />
                <Checklist
                  status={status}
                  onRecheck={onRecheck}
                  onStart={startIngestion}
                  starting={starting}
                />
                {status.error ? (
                  <Note tone="warn" icon={AlertTriangleIcon}>
                    {status.error}
                  </Note>
                ) : null}
                {startError ? (
                  <Note tone="warn" icon={AlertTriangleIcon}>
                    {startError}
                  </Note>
                ) : null}
                <p className="mt-5 text-[11.5px] leading-[17px] text-muted-foreground">
                  ReqTrace writes only inside{" "}
                  <span className="font-mono">data/</span>, and never shows a
                  stack trace: every failure here is a sentence and a next
                  action.
                </p>
              </>
            )
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

/* ------------------------------------------------------------- pieces ---- */

function Heading({ title, sub }: { title: string; sub: string }) {
  return (
    <>
      <h1 className="text-[19px] leading-[25px] font-semibold tracking-[-0.015em]">
        {title}
      </h1>
      <p className="mt-2 text-[12.5px] leading-[19px] text-muted-foreground">
        {sub}
      </p>
    </>
  );
}

function describeCorpus(status: SetupStatus): string {
  const count = status.documents.length;
  const docs =
    count > 0
      ? `${count} AUTOSAR specification${count === 1 ? "" : "s"}`
      : "The AUTOSAR specifications";
  return `${docs} and one pinned code snapshot`;
}

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

/* --------------------------------------------------- artboard A: rows ---- */

function Checklist({
  status,
  onRecheck,
  onStart,
  starting,
}: {
  status: SetupStatus;
  onRecheck: () => void;
  onStart: () => void;
  starting: boolean;
}) {
  const indexed = status.index_present && status.requirements > 0;
  const keyMissing = !status.api_key_present;
  const blocked = starting
    ? "Starting…"
    : keyMissing
      ? "Set the key first — the button stays disabled until a key is set."
      : null;

  return (
    <div className="mt-6 flex flex-col gap-2">
      <Row ok icon={ServerIcon} label="Backend reachable">
        <span className="font-mono">localhost:8000</span> ·{" "}
        <span className="font-mono">GET /setup/status</span> → 200
      </Row>

      <Row
        ok={status.api_key_present}
        alert={!status.api_key_present}
        icon={CircleAlertIcon}
        label={
          status.api_key_present
            ? "OpenRouter API key present"
            : "OPENROUTER_API_KEY is not set"
        }
      >
        {status.api_key_present ? (
          "Every model call — chat, judge, rerank, embeddings — goes through OpenRouter."
        ) : (
          <>
            Paste it below — it is saved to{" "}
            <span className="font-mono">backend/.env</span> and takes effect
            immediately. Every model call — chat, judge, rerank, embeddings —
            goes through OpenRouter.
            <KeyForm onSaved={onRecheck} />
          </>
        )}
      </Row>

      <Row
        ok={indexed}
        icon={PlayIcon}
        label={indexed ? "Corpus index" : "Corpus index not built"}
      >
        {indexed ? (
          `${status.requirements.toLocaleString()} requirements · ${status.code_units.toLocaleString()} code units · ${status.indexed_chunks.toLocaleString()} indexed chunks`
        ) : (
          <>
            {status.requirements} requirements · {status.code_units} code units
            · <span className="font-mono">data/</span> is empty. Roughly six
            minutes and a few cents of embeddings.
            <span className="mt-2 flex flex-wrap items-center gap-2.5">
              <Button size="xs" onClick={onStart} disabled={blocked !== null}>
                {starting ? (
                  <Loader2Icon className="size-3 animate-spin" />
                ) : (
                  <PlayIcon className="size-3" />
                )}
                Run ingestion
              </Button>
              {blocked ? (
                <span className="text-[11px] text-muted-foreground">
                  {blocked}
                </span>
              ) : null}
            </span>
            <Downloads status={status} />
          </>
        )}
      </Row>
    </div>
  );
}

function Row({
  ok,
  alert = false,
  icon: Icon,
  label,
  children,
}: {
  ok: boolean;
  /** A blocker, not just an unchecked box: the marker turns into a red
   *  exclamation mark. */
  alert?: boolean;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-3 rounded-lg border bg-card px-3.5 py-3">
      {ok ? (
        <span className="mt-0.5 flex size-4 flex-none items-center justify-center rounded-full bg-foreground text-background">
          <CheckIcon className="size-2.5" />
        </span>
      ) : alert ? (
        <span
          aria-label="required"
          className="mt-0.5 flex size-4 flex-none items-center justify-center text-destructive"
        >
          <CircleAlertIcon className="size-4" />
        </span>
      ) : (
        <span className="mt-0.5 flex size-4 flex-none items-center justify-center rounded-full border border-dashed text-muted-foreground">
          <Icon className="size-2.5" />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] leading-[17px] font-medium">{label}</div>
        <div className="mt-0.5 text-[11.5px] leading-[17px] text-muted-foreground">
          {children}
        </div>
      </div>
    </div>
  );
}

/**
 * Paste-a-key form. The input is a password field so the key never sits
 * readable on screen; the backend never echoes it back either. On success the
 * caller re-checks status, which flips the row to its ✓ state.
 */
function KeyForm({ onSaved }: { onSaved: () => void }) {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (!draft.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      const response = await fetch("/api/py/setup/key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: draft }),
      });
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const detail =
          typeof body === "object" && body !== null && "detail" in body
            ? String((body as { detail: unknown }).detail)
            : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      setDraft("");
      onSaved();
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "Could not save the key — is the backend still running?",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <span className="mt-2 block">
      <span className="flex items-center gap-2">
        <Input
          type="password"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void save();
          }}
          placeholder="sk-or-v1-…"
          aria-label="OpenRouter API key"
          autoComplete="off"
          className="h-7 max-w-[280px] font-mono text-[11px]"
        />
        <Button size="xs" onClick={() => void save()} disabled={!draft.trim() || saving}>
          {saving ? <Loader2Icon className="size-3 animate-spin" /> : null}
          Save key
        </Button>
      </span>
      {error ? (
        <span className="mt-1.5 block text-[11px] text-destructive">{error}</span>
      ) : null}
    </span>
  );
}

/** The canvas's "What gets downloaded" disclosure — what, from where. */
function Downloads({ status }: { status: SetupStatus }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="mt-2 block">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex items-center gap-1 text-[11px] font-medium text-foreground hover:underline"
      >
        {open ? (
          <ChevronDownIcon className="size-3" />
        ) : (
          <ChevronRightIcon className="size-3" />
        )}
        What gets downloaded
      </button>
      {open ? (
        <span className="mt-1.5 block rounded-md bg-muted/60 px-2.5 py-2 text-[11px] leading-[17px]">
          {status.documents.length > 0 ? (
            <>
              {status.documents.length} SWS PDFs from autosar.org:{" "}
              {status.documents.map((doc) => doc.title).join(", ")}.
            </>
          ) : (
            "The AUTOSAR SWS PDFs named in the project manifest, from autosar.org."
          )}{" "}
          Plus the pinned code repository
          {status.git_sha ? (
            <>
              {" "}
              at <span className="font-mono">{status.git_sha.slice(0, 7)}</span>
            </>
          ) : null}
          {status.snapshot_present ? " (already on disk)" : ""}. Everything
          lands inside <span className="font-mono">data/</span>.
        </span>
      ) : null}
    </span>
  );
}

/* ------------------------------------------- artboard B: the tracker ---- */

const STEP_TOTAL = 5;

function Tracker({
  status,
  steps,
  lines,
  phase,
  running,
  restartRequired,
  logError,
  opening,
  openError,
  onOpen,
  onCancel,
  onRetry,
  onRecheck,
}: {
  status: SetupStatus;
  steps: IngestStep[];
  lines: string[];
  phase: string;
  running: boolean;
  restartRequired: boolean;
  logError: string | null;
  opening: boolean;
  openError: string | null;
  onOpen: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onRecheck: () => void;
}) {
  const jobStatus = status.ingest.status;
  const failed = jobStatus === "failed" || phase === "failed";
  const cancelled = jobStatus === "cancelled" || phase === "cancelled";
  const doneCount = steps.filter((step) => step.status === "done").length;
  const runningIndex = steps.findIndex((step) => step.status === "running");
  const stepNumber = runningIndex >= 0 ? runningIndex + 1 : doneCount;

  return (
    <>
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="text-[19px] leading-[25px] font-semibold tracking-[-0.015em]">
          {failed
            ? "Ingestion failed"
            : cancelled
              ? "Ingestion cancelled"
              : restartRequired
                ? "Index built"
                : "Building the index"}
        </h1>
        {running && stepNumber > 0 ? (
          <Meta className="flex-none">
            step {stepNumber} of {STEP_TOTAL}
          </Meta>
        ) : null}
      </div>
      <p className="mt-2 text-[12.5px] leading-[19px] text-muted-foreground">
        You can leave this open. Every step is idempotent — re-running skips
        whatever is already on disk.
      </p>

      {steps.length > 0 ? (
        <ol className="mt-6 flex flex-col gap-1">
          {steps.map((step) => (
            <li key={step.key} className="flex items-start gap-3 py-1">
              <StepDot status={step.status} />
              <div className="min-w-0 flex-1">
                <span
                  className={
                    step.status === "pending"
                      ? "text-[12.5px] leading-[17px] text-muted-foreground"
                      : "text-[12.5px] leading-[17px] font-medium"
                  }
                >
                  {step.label}
                </span>
                {step.detail ? (
                  <span className="ml-2 font-mono text-[10.5px] text-muted-foreground">
                    {step.detail}
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      ) : null}

      {lines.length > 0 ? <IngestLog lines={lines} /> : null}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {running ? (
          <>
            <Elapsed since={status.ingest.started_at} />
            <Button size="sm" variant="outline" onClick={onCancel}>
              <XIcon className="size-3.5" />
              Cancel
            </Button>
          </>
        ) : failed || cancelled ? (
          <Button size="sm" onClick={onRetry}>
            <RotateCcwIcon className="size-3.5" />
            {failed ? "Retry — finished steps are kept" : "Run again"}
          </Button>
        ) : restartRequired ? (
          <>
            <Button size="sm" onClick={onOpen} disabled={opening}>
              {opening ? (
                <Loader2Icon className="size-3.5 animate-spin" />
              ) : (
                <ArrowRightIcon className="size-3.5" />
              )}
              Open ReqTrace
            </Button>
            <span className="text-[11.5px] text-muted-foreground">
              The server loads the new index in place — nothing to restart.
            </span>
          </>
        ) : null}
        <Button size="sm" variant="outline" onClick={onRecheck}>
          <RefreshCwIcon className="size-3.5" />
          Re-check
        </Button>
      </div>

      {failed && status.ingest.error && status.ingest.error !== "cancelled" ? (
        <Note tone="warn" icon={AlertTriangleIcon}>
          <strong>
            {steps.find((step) => step.status === "failed")?.label ??
              "Ingestion"}{" "}
            failed.
          </strong>{" "}
          {status.ingest.error} Check the cause, then retry — the finished
          steps are kept.
        </Note>
      ) : null}
      {logError ? (
        <Note tone="warn" icon={AlertTriangleIcon}>
          {logError}
        </Note>
      ) : null}
      {openError ? (
        // The in-place reload failed — fall back to the old manual path.
        <Note tone="warn" icon={AlertTriangleIcon}>
          Could not load the new index in place ({openError}). Fall back to a
          restart: stop <code className="font-mono">make dev</code> and run it
          again, then reload this page.
        </Note>
      ) : null}
    </>
  );
}

function StepDot({ status }: { status: IngestStep["status"] }) {
  if (status === "done")
    return (
      <span className="mt-0.5 flex size-4 flex-none items-center justify-center rounded-full bg-foreground text-background">
        <CheckIcon className="size-2.5" />
      </span>
    );
  if (status === "running")
    return (
      <span className="mt-0.5 flex size-4 flex-none items-center justify-center">
        <Loader2Icon className="size-3.5 animate-spin" />
      </span>
    );
  if (status === "failed")
    return (
      <span className="mt-0.5 flex size-4 flex-none items-center justify-center text-destructive">
        <AlertTriangleIcon className="size-3.5" />
      </span>
    );
  return (
    <span className="mt-0.5 flex size-4 flex-none items-center justify-center">
      <span className="size-2 rounded-full border border-dashed" />
    </span>
  );
}

/** The live clock — "1 m 12 s elapsed", ticking from the job's own start. */
function Elapsed({ since }: { since: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  if (!since) return null;
  const seconds = Math.max(0, Math.floor((now - Date.parse(since)) / 1000));
  const text =
    seconds >= 60 ? `${Math.floor(seconds / 60)} m ${seconds % 60} s` : `${seconds} s`;
  return (
    <Meta>
      {text} <span className="opacity-70">elapsed</span>
    </Meta>
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
      <div className="mt-2 max-h-[240px] overflow-y-auto rounded-lg border bg-muted/40 p-3">
        {lines.map((line, index) => (
          <div
            key={index}
            className="font-mono text-[11px] leading-[17px] whitespace-pre-wrap"
          >
            {line || " "}
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
