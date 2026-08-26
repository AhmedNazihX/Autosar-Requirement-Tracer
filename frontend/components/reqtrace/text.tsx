import { cn } from "@/lib/utils";

/**
 * The canvas's `.cap` label: 10.5 px, 600, 0.09 em, uppercase, muted. It labels
 * the Sources / Upstream rows, the evidence panel sections and the tab crumbs,
 * so it is one component rather than the same six utilities repeated.
 */
export function Cap({
  className,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "text-[10.5px] leading-[14px] font-semibold tracking-[0.09em] uppercase text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

/** The canvas's `.mono` metadata size: 10.5 px, tabular, muted by default. */
export function Meta({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "font-mono text-[10.5px] leading-[15px] text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

/** The canvas's `.kbd`: a keycap in the composer hint row. */
export function Kbd({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="kbd"
      className={cn(
        "inline-block rounded-[4px] border bg-muted px-1 text-[10px] leading-[13px]",
        className,
      )}
      {...props}
    />
  );
}
