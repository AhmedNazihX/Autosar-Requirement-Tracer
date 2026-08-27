"use client";

import { Loader2Icon } from "lucide-react";

import type { HighlightedFile } from "@/lib/code-highlight";
import { chatSourceKind } from "@/lib/chat-sources";
import { useSetupStatus } from "@/hooks/use-setup-status";

import { AppShell } from "./app-shell";
import { SetupScreen } from "./setup/setup-screen";

/**
 * What the user sees first: the app, or the reason they cannot have it yet.
 *
 * WP5 made `live` the default chat source, which means the very first render
 * now depends on something outside the browser. `GET /setup/status` answers it
 * before anything else mounts, so a missing index or key reaches the user as
 * the guided setup screen (story S5.6.1) rather than as a chat pane that
 * errors on the first message.
 *
 * **Canned mode skips the gate entirely.** The fixture needs no backend, no
 * index and no key — gating it behind a readiness check would break the one
 * thing it exists for.
 *
 * The gate deliberately does *not* fall back to canned when the backend is
 * missing. Silently swapping a real conversation for a scripted one is the
 * worst available behaviour: everything appears to work, the answers look
 * plausible, and nothing says they were fixtures.
 */
export function BootGate({ codeFile }: { codeFile: HighlightedFile }) {
  const canned = chatSourceKind() === "canned";
  const { state, refresh } = useSetupStatus();

  if (canned) return <AppShell codeFile={codeFile} />;

  if (state.phase === "checking") {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-background">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          <span className="text-[12.5px]">Starting ReqTrace…</span>
        </div>
      </main>
    );
  }

  if (state.phase === "unreachable") {
    return (
      <SetupScreen status={null} unreachable onRecheck={refresh} />
    );
  }

  if (!state.status.ready) {
    return (
      <SetupScreen status={state.status} onRecheck={refresh} />
    );
  }

  return <AppShell codeFile={codeFile} setup={state.status} />;
}
