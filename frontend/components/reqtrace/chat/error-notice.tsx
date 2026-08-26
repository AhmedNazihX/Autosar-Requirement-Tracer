"use client";

import { CircleAlertIcon, RefreshCwIcon } from "lucide-react";

import type { ErrorEventData } from "@/lib/events";
import { Button } from "@/components/ui/button";

/**
 * An `error` SSE event, rendered inline.
 *
 * This is the only red in a message. The canvas's `verdict-colour` note is
 * explicit that the destructive tint belongs to genuine failures — a stream that
 * died, an unreachable backend — and never to a verdict or an empty result.
 *
 * The message keeps whatever streamed before the failure. The point of showing
 * this inline as well as in a toast is that the toast disappears and the thread
 * still has to explain itself on reload.
 */
export function ErrorNotice({
  error,
  onRetry,
}: {
  error: ErrorEventData;
  onRetry?: () => void;
}) {
  return (
    <div className="rounded-lg border border-destructive-border bg-destructive-bg px-3 py-2.5">
      <div className="flex items-start gap-2.5">
        <CircleAlertIcon className="mt-px size-3.5 flex-none text-destructive" />
        <div className="flex-1">
          <p className="text-[12.5px] leading-[17px] font-medium">
            The answer stopped mid-stream
          </p>
          <p className="mt-0.5 text-[11px] leading-[15.5px] text-muted-foreground">
            {error.message}
          </p>
        </div>
        {error.retryable && onRetry ? (
          <Button variant="outline" size="xs" onClick={onRetry}>
            <RefreshCwIcon />
            Retry
          </Button>
        ) : null}
      </div>
    </div>
  );
}
