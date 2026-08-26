"use client";

/**
 * The collapse order from the canvas's `layout-rules` note, in one place.
 *
 *   < 1280  the source pane becomes an overlay sheet, opened by a citation
 *           click and dismissed with Escape
 *   < 1100  the thread sidebar collapses to an icon rail; the checkpoint rail
 *           STAYS — it is the cheaper of the two to keep and the harder to
 *           re-find
 *   < 900   the checkpoint rail folds into a popover on the thread title
 *
 * Reported as booleans rather than a breakpoint name so a component asks for
 * the one fact it needs.
 */

import { useEffect, useState } from "react";

export interface Viewport {
  width: number | null;
  /** Source pane is an overlay rather than a column. */
  sourceIsOverlay: boolean;
  /** Thread sidebar is a 44 px icon rail rather than a 264 px column. */
  sidebarIsRail: boolean;
  /** Checkpoint rail is folded into a popover on the thread title. */
  railIsPopover: boolean;
}

const WIDE: Viewport = {
  width: null,
  sourceIsOverlay: false,
  sidebarIsRail: false,
  railIsPopover: false,
};

function measure(width: number): Viewport {
  return {
    width,
    sourceIsOverlay: width < 1280,
    sidebarIsRail: width < 1100,
    railIsPopover: width < 900,
  };
}

export function useViewport(): Viewport {
  // The server has no width. Rendering the wide layout first and correcting on
  // mount matches the canvas's default (1440) and avoids a hydration mismatch.
  const [viewport, setViewport] = useState<Viewport>(WIDE);

  useEffect(() => {
    const update = () => setViewport(measure(window.innerWidth));
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  return viewport;
}
