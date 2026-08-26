"use client";

import { useEffect, useState } from "react";

type HealthState =
  | { status: "checking" }
  | { status: "ok"; version: string }
  | { status: "unreachable" };

export default function Home() {
  const [health, setHealth] = useState<HealthState>({ status: "checking" });

  useEffect(() => {
    let cancelled = false;

    fetch("/api/py/health")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: { status: string; version: string }) => {
        if (!cancelled) setHealth({ status: "ok", version: data.version });
      })
      .catch(() => {
        if (!cancelled) setHealth({ status: "unreachable" });
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-zinc-50 px-6 font-sans dark:bg-black">
      <main className="flex w-full max-w-xl flex-col items-center gap-6 text-center">
        <h1 className="text-4xl font-semibold tracking-tight text-black dark:text-zinc-50">
          ReqTrace
        </h1>
        <p className="max-w-md text-lg leading-8 text-zinc-600 dark:text-zinc-400">
          A requirements-to-code traceability chatbot for automotive
          embedded software.
        </p>
        <div
          data-testid="backend-status"
          className="rounded-full border border-black/[.08] px-4 py-2 text-sm font-medium dark:border-white/[.145]"
        >
          Backend:{" "}
          {health.status === "checking" && "checking..."}
          {health.status === "ok" && `ok (v${health.version})`}
          {health.status === "unreachable" && "not reachable"}
        </div>
      </main>
    </div>
  );
}
