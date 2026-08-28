"use client";

import { CodeIcon, FileTextIcon, HistoryIcon, XIcon } from "lucide-react";

import type { HighlightedFile } from "@/lib/code-highlight";
import type { RequirementCitation } from "@/lib/events";
import type { SourceTab, SourceTarget } from "@/lib/source-target";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { CodeTab } from "./code-tab";
import { DocumentTab } from "./document-tab";

/**
 * The source pane: two tabs over one target.
 *
 * The target is always a `citation` event — never a path the user typed, never a
 * file picker. That is the whole security posture of this pane in one sentence:
 * it can only open what the indexed snapshot already told the chat about.
 *
 * Tab state is lifted, because a citation click has to be able to switch tabs.
 */
/**
 * A dot on a tab that has something in it.
 *
 * Clicking a requirement now fills the code tab too, and without a marker
 * nobody finds out: the document opens, the code sits behind an unchanged
 * label, and the connection the pane just made is invisible.
 */
function Loaded() {
  return (
    <span
      aria-hidden
      className="ml-0.5 size-1 flex-none rounded-full bg-source"
      data-slot="tab-loaded"
    />
  );
}

export function SourcePane({
  target,
  tab,
  onTabChange,
  onClose,
  codeFile,
  onOpenRequirement,
  restoredFrom = null,
}: {
  target: SourceTarget | null;
  tab: SourceTab;
  onTabChange: (tab: SourceTab) => void;
  onClose: () => void;
  codeFile: HighlightedFile;
  /** Open a requirement the open code traces to (story: the reverse link). */
  onOpenRequirement?: (citation: RequirementCitation) => void;
  /** Set while a checkpoint restore is pinning the pane to an earlier turn
   *  (story S5.4.1). Without it the pane silently shows an old page while the
   *  conversation has moved on — which reads as a bug, not as a rewind. */
  restoredFrom?: { turn: number; onReturn: () => void } | null;
}) {
  // Both tabs render from whichever citations the target carries. A chat chip
  // supplies one; a report row supplies both (see `SourceTarget.companion`).
  const carried = [target?.citation, target?.companion].filter(
    (one) => one != null,
  );
  const requirement =
    carried.find((one) => one.kind === "requirement") ?? null;
  const code = carried.find((one) => one.kind === "code") ?? null;

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => onTabChange(value as SourceTab)}
      className="flex min-h-0 flex-1 flex-col gap-0"
    >
      <div className="flex h-10 flex-none items-center border-b pr-1.5 pl-2">
        <TabsList
          variant="line"
          className="h-10 gap-0 rounded-none bg-transparent p-0"
        >
          <TabsTrigger
            value="document"
            className="h-10 flex-none gap-1.5 px-3 text-[12.8px] [&::after]:bottom-0!"
          >
            <FileTextIcon className="size-3.5" />
            Document
            {requirement ? <Loaded /> : null}
          </TabsTrigger>
          <TabsTrigger
            value="code"
            className="h-10 flex-none gap-1.5 px-3 text-[12.8px] [&::after]:bottom-0!"
          >
            <CodeIcon className="size-3.5" />
            Code
            {code ? <Loaded /> : null}
          </TabsTrigger>
        </TabsList>
        <span className="flex-1" />
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onClose}
          aria-label="Close source pane"
        >
          <XIcon />
        </Button>
      </div>

      {restoredFrom ? (
        <div className="flex h-8 flex-none items-center gap-2 border-b bg-muted px-3">
          <HistoryIcon className="size-3.5 flex-none text-muted-foreground" />
          <span className="flex-1 text-[11.5px] text-muted-foreground">
            Restored from turn {restoredFrom.turn}
          </span>
          <Button variant="ghost" size="xs" onClick={restoredFrom.onReturn}>
            Return to latest
          </Button>
        </div>
      ) : null}

      <TabsContent
        value="document"
        className="flex min-h-0 flex-1 flex-col overflow-hidden"
      >
        <DocumentTab citation={requirement} />
      </TabsContent>

      <TabsContent
        value="code"
        className="flex min-h-0 flex-1 flex-col overflow-hidden"
      >
        <CodeTab
          file={codeFile}
          citation={code}
          // The pair's other half: names the evidence pill and keys the
          // Evidence card, whether it arrived clicked or as a companion.
          requirement={requirement}
          onOpenRequirement={onOpenRequirement}
        />
      </TabsContent>
    </Tabs>
  );
}
