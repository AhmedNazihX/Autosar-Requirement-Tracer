"use client";

import { useEffect, useMemo, useState } from "react";
import { Loader2Icon, PlayIcon, TriangleAlertIcon } from "lucide-react";

import {
  estimateReport,
  type EstimateResponse,
  type ReportScope,
} from "@/lib/reports";
import type { DocumentStatus } from "@/hooks/use-setup-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { Cap, Meta } from "@/components/reqtrace/text";

/**
 * The launch form (story S5.5.1) — scope, estimate, go.
 *
 * **The estimate is a form concern, not a run phase.** `POST /reports/estimate`
 * is pure — it costs nothing and starts nothing — so the form calls it itself,
 * debounced, on every scope change, and `useReport` never hears about it. The
 * figures the user launches with are therefore the figures they were shown.
 */

type ScopeMode = "modules" | "document" | "req_ids";

const MODES: readonly { key: ScopeMode; label: string }[] = [
  { key: "modules", label: "By module" },
  { key: "document", label: "By document" },
  { key: "req_ids", label: "By requirement ID" },
];

/** How long a scope must hold still before it is estimated. Typing a
 *  requirement id fires a keystroke per character; the backend does not need
 *  to hear every one of them. */
const ESTIMATE_DEBOUNCE_MS = 400;

/** The judge's measured pace, for the "~N min" hint. Approximate on purpose —
 *  the `~` is part of the contract. */
const SECONDS_PER_VERDICT = 1.3;

export function LaunchForm({
  busy,
  defaultModule,
  documents,
  gitSha,
  onLaunch,
}: {
  busy: boolean;
  defaultModule: string;
  /** From `GET /setup/status` — one row per manifest document. */
  documents: readonly DocumentStatus[];
  /** The pinned snapshot SHA, for the cache captions. `null` in canned mode. */
  gitSha: string | null;
  onLaunch: (scope: ReportScope) => void;
}) {
  const [mode, setMode] = useState<ScopeMode>("modules");
  const [selected, setSelected] = useState<ReadonlySet<string>>(
    () => new Set([defaultModule]),
  );
  const [documentKey, setDocumentKey] = useState<string | null>(null);
  const [reqIdsText, setReqIdsText] = useState("");
  const [rejudge, setRejudge] = useState(false);
  const [limit, setLimit] = useState("");

  const parsedLimit = limit.trim() ? Number(limit) : null;
  const limitValid =
    parsedLimit === null || (Number.isInteger(parsedLimit) && parsedLimit > 0);

  // Two documents may share a module; the module list dedupes and sums so a
  // checkbox never appears twice. Order is the manifest's.
  const moduleRows = useMemo(() => {
    const byModule = new Map<string, { title: string; requirements: number }>();
    for (const doc of documents) {
      const row = byModule.get(doc.module);
      if (row) row.requirements += doc.requirements;
      else byModule.set(doc.module, { title: doc.title, requirements: doc.requirements });
    }
    return [...byModule].map(([module, row]) => ({ module, ...row }));
  }, [documents]);

  const reqIds = useMemo(
    () => reqIdsText.split(/[\s,]+/).filter(Boolean),
    [reqIdsText],
  );

  /** `null` while the scope is incomplete — nothing to estimate or launch. */
  const scope = useMemo<ReportScope | null>(() => {
    const shared = { rejudge, limit: limitValid ? parsedLimit : null };
    if (mode === "modules") {
      const modules = moduleRows
        .map((row) => row.module)
        .filter((name) => selected.has(name));
      return modules.length ? { modules, ...shared } : null;
    }
    if (mode === "document")
      return documentKey ? { document: documentKey, ...shared } : null;
    return reqIds.length ? { req_ids: reqIds, ...shared } : null;
  }, [mode, moduleRows, selected, documentKey, reqIds, rejudge, limitValid, parsedLimit]);

  // The scope as a stable string, so the effect below has one dependency and
  // an unchanged scope re-serialised never re-fires it.
  const scopeKey = scope ? JSON.stringify(scope) : "";

  const [estimate, setEstimate] = useState<{
    key: string;
    data: EstimateResponse | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    if (!scopeKey) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      estimateReport(JSON.parse(scopeKey) as ReportScope)
        .then((data) => {
          if (!controller.signal.aborted)
            setEstimate({ key: scopeKey, data, error: null });
        })
        .catch((cause: unknown) => {
          if (!controller.signal.aborted)
            setEstimate({
              key: scopeKey,
              data: null,
              error:
                cause instanceof Error
                  ? cause.message
                  : "The scope could not be estimated.",
            });
        });
    }, ESTIMATE_DEBOUNCE_MS);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [scopeKey]);

  // Staleness is derived, not set: an estimate for a scope the user has since
  // edited renders dimmed until its replacement lands. No setState in the
  // effect body, per this React's `set-state-in-effect` rule.
  const current = estimate && estimate.key === scopeKey ? estimate : null;
  const lastData = estimate?.data ?? null;
  const pending = Boolean(scopeKey) && !current;

  const sha7 = gitSha ? gitSha.slice(0, 7) : null;
  const scopesCan =
    mode === "modules"
      ? selected.has("Can")
      : mode === "document"
        ? documents.find((doc) => doc.key === documentKey)?.module === "Can"
        : false;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="max-w-[560px]">
          <Cap>Scope</Cap>

          <div className="mt-2 flex w-max gap-0.5 rounded-lg bg-muted p-0.5">
            {MODES.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                onClick={() => setMode(key)}
                className={
                  key === mode
                    ? "rounded-md bg-card px-2.5 py-1 text-[12px] font-medium shadow-xs"
                    : "rounded-md px-2.5 py-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
                }
              >
                {label}
              </button>
            ))}
          </div>

          {mode === "modules" ? (
            <div className="mt-2">
              {moduleRows.map(({ module, title, requirements }) => (
                <label
                  key={module}
                  className="flex cursor-pointer items-center gap-2.5 py-1.5"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(module)}
                    onChange={(event) => {
                      const next = new Set(selected);
                      if (event.target.checked) next.add(module);
                      else next.delete(module);
                      setSelected(next);
                    }}
                    className="size-3.5 flex-none accent-foreground"
                  />
                  <span className="w-14 flex-none font-mono text-[12px] font-medium">
                    {module}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[12.5px]">{title}</span>
                  {requirements > 0 ? (
                    <Meta className="text-[11.5px]">{requirements} requirements</Meta>
                  ) : null}
                </label>
              ))}
            </div>
          ) : null}

          {mode === "document" ? (
            <div className="mt-2">
              {documents.map((doc) => (
                <label
                  key={doc.key}
                  className="flex cursor-pointer items-center gap-2.5 py-1.5"
                >
                  <input
                    type="radio"
                    name="report-document"
                    checked={documentKey === doc.key}
                    onChange={() => setDocumentKey(doc.key)}
                    className="size-3.5 flex-none accent-foreground"
                  />
                  <span className="w-14 flex-none font-mono text-[12px] font-medium">
                    {doc.module}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[12.5px]">{doc.title}</span>
                  {doc.requirements > 0 ? (
                    <Meta className="text-[11.5px]">{doc.requirements} requirements</Meta>
                  ) : null}
                </label>
              ))}
            </div>
          ) : null}

          {mode === "req_ids" ? (
            <div className="mt-2">
              <Textarea
                value={reqIdsText}
                onChange={(event) => setReqIdsText(event.target.value)}
                placeholder={"SWS_Can_00011, SWS_CANIF_00219 …"}
                aria-label="Requirement IDs"
                className="min-h-20 font-mono text-[11.5px]"
              />
              <p className="mt-1.5 text-[11px] leading-[15.5px] text-muted-foreground">
                Whitespace or commas — either separates. Ids the corpus does not
                know are reported below, not silently dropped.
              </p>
            </div>
          ) : null}

          {scopesCan ? (
            <p className="mt-2 text-[11.5px] leading-[17px] text-muted-foreground">
              <span className="text-foreground">
                The CAN Driver has no implementation in this repository
              </span>{" "}
              — that scope is the release-drift demonstration, and it will come
              back almost entirely <span className="font-mono">missing</span>.
            </p>
          ) : null}

          <div className="my-4 h-px bg-border" />

          <div className="flex flex-col gap-3">
            <label className="flex items-start gap-2.5">
              <input
                type="checkbox"
                checked={rejudge}
                onChange={(event) => setRejudge(event.target.checked)}
                className="mt-0.5 size-3.5 flex-none accent-foreground"
              />
              <span className="text-[12.5px] leading-[17px]">
                Re-judge cached verdicts
                <span className="block text-[11.5px] text-muted-foreground">
                  {lastData ? (
                    <>
                      {lastData.cached.toLocaleString()} verdicts are cached
                      {sha7 ? (
                        <>
                          {" "}
                          for <span className="font-mono">{sha7}</span> + the
                          pinned judge model
                        </>
                      ) : null}
                      .
                    </>
                  ) : (
                    <>
                      Off by default. Verdicts are cached per requirement,
                      snapshot and judge model, so a re-run of an unchanged
                      scope normally costs nothing at all.
                    </>
                  )}
                </span>
              </span>
            </label>

            <label className="flex items-center gap-2.5">
              <span className="text-[12.5px]">Stop after</span>
              <Input
                value={limit}
                onChange={(event) => setLimit(event.target.value)}
                placeholder="all"
                inputMode="numeric"
                className="h-7 w-24 font-mono text-[11.5px]"
                aria-label="Requirement limit"
              />
              <span className="text-[11.5px] text-muted-foreground">
                requirements — for a quick partial run.
              </span>
            </label>
            {/* The canvas's rule: a disabled control says why, in text. */}
            {!limitValid ? (
              <p className="text-[11.5px] text-muted-foreground">
                The limit must be a whole number above zero.
              </p>
            ) : null}
          </div>

          <EstimateBox
            scoped={Boolean(scope)}
            pending={pending}
            data={current?.data ?? lastData}
            error={current?.error ?? null}
            sha7={sha7}
            showUnknownIds={mode === "req_ids"}
          />
        </div>
      </div>

      <div className="flex flex-none items-center gap-3 border-t px-6 py-3">
        <p className="flex-1 text-[10.5px] leading-[14px] text-muted-foreground">
          The run aborts at the ceiling and keeps the verdicts it already has.
        </p>
        <Button
          size="sm"
          disabled={busy || !limitValid || !scope}
          onClick={() => scope && onLaunch(scope)}
        >
          {busy ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PlayIcon className="size-3.5" />
          )}
          Generate report
        </Button>
      </div>
    </div>
  );
}

function EstimateBox({
  scoped,
  pending,
  data,
  error,
  sha7,
  showUnknownIds,
}: {
  scoped: boolean;
  /** An estimate for the current scope is in flight; `data` may be a stale
   *  predecessor, rendered dimmed rather than blanked. */
  pending: boolean;
  data: EstimateResponse | null;
  error: string | null;
  sha7: string | null;
  showUnknownIds: boolean;
}) {
  if (!scoped) {
    return (
      <div className="mt-4 rounded-[10px] bg-muted px-3 py-2.5">
        <p className="text-[11.5px] text-muted-foreground">
          Pick a scope to see what it will cost.
        </p>
      </div>
    );
  }

  if (!data && pending) {
    return (
      <div className="mt-4 flex flex-col gap-2 rounded-[10px] bg-muted px-3 py-2.5">
        {/* `bg-background`: the default skeleton is `bg-muted`, invisible on
            this box's own muted ground. */}
        <Skeleton className="h-3.5 w-2/3 bg-background" />
        <Skeleton className="h-3.5 w-1/2 bg-background" />
        <Skeleton className="h-5 w-1/3 bg-background" />
      </div>
    );
  }

  if (!data) {
    // The estimate failing must not block the launch: the ceiling is enforced
    // server-side either way, and the error sentence names what went wrong.
    return (
      <div className="mt-4 rounded-[10px] bg-muted px-3 py-2.5">
        <p className="text-[11.5px] leading-[16px] text-muted-foreground">
          {error ?? "The scope could not be estimated."} You can still launch —
          the cost ceiling applies regardless.
        </p>
      </div>
    );
  }

  const ceilingShare =
    data.est_cost_usd !== null && data.ceiling_usd > 0
      ? Math.min(100, Math.round((100 * data.est_cost_usd) / data.ceiling_usd))
      : null;

  return (
    <div
      className={`mt-4 rounded-[10px] bg-muted px-3 py-2.5 transition-opacity ${pending ? "opacity-60" : ""}`}
    >
      <EstimateRow label="Requirements in scope" value={data.requirements.toLocaleString()} />
      <EstimateRow
        label={sha7 ? `Cached at ${sha7}` : "Cached"}
        value={`−${data.cached.toLocaleString()}`}
      />
      <EstimateRow label="To judge" value={data.to_judge.toLocaleString()} bold />
      <div className="my-2 h-px bg-border" />
      <div className="flex items-end justify-between gap-3">
        <span className="text-[12.5px] font-medium">Estimated cost</span>
        <span className="font-mono text-[17px] leading-5 font-semibold">
          {data.est_cost_usd === null ? "unavailable" : `$${data.est_cost_usd.toFixed(2)}`}
        </span>
      </div>
      {data.est_cost_usd === null ? (
        <p className="mt-1 text-[10.5px] leading-[14px] text-muted-foreground">
          {data.est_basis}
        </p>
      ) : (
        <>
          <div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-background">
            <div
              className="rounded-full bg-foreground"
              style={{ width: `${ceilingShare ?? 0}%` }}
            />
          </div>
          <div className="mt-1.5 flex items-baseline gap-3">
            <span className="flex-1 text-[10.5px] leading-[14px] text-muted-foreground">
              {ceilingShare}% of the{" "}
              <span className="font-mono">MAX_REPORT_COST_USD</span> ceiling ($
              {data.ceiling_usd.toFixed(2)})
            </span>
            <Meta>{judgingTime(data.to_judge)}</Meta>
          </div>
        </>
      )}
      {showUnknownIds && data.unknown_ids.length > 0 ? (
        <div className="mt-2.5 flex items-start gap-2 rounded-lg border border-verdict-partial-border bg-verdict-partial-bg px-2.5 py-2 text-verdict-partial">
          <TriangleAlertIcon className="mt-0.5 size-3.5 flex-none" />
          <p className="text-[11px] leading-[15.5px]">
            {data.unknown_ids.length === 1 ? "This id is" : "These ids are"} not
            in the corpus and will not be judged:{" "}
            <span className="font-mono">{data.unknown_ids.join(", ")}</span>
          </p>
        </div>
      ) : null}
    </div>
  );
}

function EstimateRow({
  label,
  value,
  bold = false,
}: {
  label: string;
  value: string;
  bold?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between py-0.5">
      <span
        className={
          bold ? "text-[11.5px] font-medium" : "text-[11.5px] text-muted-foreground"
        }
      >
        {label}
      </span>
      <span className={`font-mono text-[11.5px] tabular-nums ${bold ? "font-medium" : ""}`}>
        {value}
      </span>
    </div>
  );
}

/** `~3 min 40 s`, from the measured per-verdict pace. */
function judgingTime(toJudge: number): string {
  const seconds = Math.round(toJudge * SECONDS_PER_VERDICT);
  if (seconds < 60) return `~${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `~${minutes} min ${rest} s` : `~${minutes} min`;
}
