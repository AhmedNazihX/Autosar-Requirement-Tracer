"use client";

import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";

/**
 * One rendered PDF page, with the citation's bbox drawn over it (story S5.3.1).
 *
 * **Why pdf.js and not a server-rendered image.** The backend never rasterises
 * a page — `GET /documents/{doc}/view` returns page and bbox *coordinates*
 * (spec §6) and `GET /documents/{doc}/file` returns the PDF itself. Rendering
 * client-side keeps the highlight geometrically exact at any zoom, and keeps a
 * 2 MB specification out of the backend's response path on every citation
 * click.
 *
 * **Documents are cached per key, and that matters.** The CAN Interface PDF is
 * 2.3 MB and 228 pages. Clicking through six citations in one document must
 * not fetch it six times, so the `PDFDocumentProxy` is memoised in a module
 * map — the page render is cheap, the document load is not.
 *
 * **The bbox is PyMuPDF's, and its origin is already top-left.** Ingestion
 * stores rectangles as PyMuPDF reports them, which is a y-down space matching
 * the page rect — *not* PDF user space, which is y-up. So the overlay scales
 * the stored rectangle and does no flip. Applying pdf.js's
 * `convertToViewportPoint` here would flip it a second time and put every
 * highlight the same distance from the wrong edge, which looks plausible on a
 * page whose paragraph happens to sit near the middle. Verified against real
 * pages rather than reasoned about.
 *
 * A `bbox` of `null` is normal: ~6% of requirements cross a page break and
 * ingestion stores no rectangle rather than a wrong one. The page still opens.
 */

/** Loaded documents, by manifest key. Module-level: two panes on the same
 *  document should share one 2 MB download. */
const documents = new Map<string, Promise<PDFDocumentProxy>>();

type PdfjsModule = typeof import("pdfjs-dist");
let pdfjs: Promise<PdfjsModule> | null = null;

function library(): Promise<PdfjsModule> {
  pdfjs ??= import("pdfjs-dist").then((module) => {
    // The worker ships with the package. Resolving it through `new URL` lets
    // the bundler fingerprint and serve it; a bare string would 404 in a
    // production build, where nothing is served from node_modules.
    module.GlobalWorkerOptions.workerSrc = new URL(
      "pdfjs-dist/build/pdf.worker.min.mjs",
      import.meta.url,
    ).toString();
    return module;
  });
  return pdfjs;
}

function documentFor(doc: string): Promise<PDFDocumentProxy> {
  const existing = documents.get(doc);
  if (existing) return existing;
  const loading = library().then((module) =>
    module.getDocument({ url: `/api/py/documents/${encodeURIComponent(doc)}/file` })
      .promise,
  );
  // A failed load must not be cached, or a transient error becomes permanent
  // for the life of the tab.
  loading.catch(() => documents.delete(doc));
  documents.set(doc, loading);
  return loading;
}

/** Reported only once the document is known, or once it has failed. There is
 *  deliberately no "loading" status: emitting one from the effect body would
 *  set the parent's state synchronously during an effect, and the parent
 *  already has `citation.page_count` to show meanwhile. */
export type PdfStatus =
  | { phase: "ready"; pages: number }
  | { phase: "failed"; message: string };

export function PdfPage({
  doc,
  page,
  bbox,
  scale,
  onStatus,
}: {
  doc: string;
  page: number;
  /** PyMuPDF's rectangle in page points, y-down. `null` when the requirement
   *  crosses a page break. */
  bbox: readonly [number, number, number, number] | null;
  scale: number;
  onStatus?: (status: PdfStatus) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  // Keyed by the inputs that produced it, so a new page clears a stale error
  // without an effect writing state during render (React's
  // `set-state-in-effect` rule) — the failure simply stops matching.
  const [failure, setFailure] = useState<{ token: string; message: string } | null>(
    null,
  );
  const token = `${doc}:${page}:${scale}`;
  const failed = failure?.token === token ? failure.message : null;

  useEffect(() => {
    let cancelled = false;
    // pdf.js rejects a second render onto a canvas whose first render is still
    // running, which is exactly what a fast click-through produces.
    let task: { cancel: () => void } | null = null;

    documentFor(doc)
      .then(async (pdf) => {
        if (cancelled) return;
        onStatus?.({ phase: "ready", pages: pdf.numPages });
        const clamped = Math.min(Math.max(page, 1), pdf.numPages);
        const rendered = await pdf.getPage(clamped);
        if (cancelled) return;

        const viewport = rendered.getViewport({ scale });
        const canvas = canvasRef.current;
        if (!canvas) return;

        // Render at device resolution so 10 pt body text is legible, then let
        // CSS lay it out at the logical size.
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * ratio);
        canvas.height = Math.floor(viewport.height * ratio);
        const context = canvas.getContext("2d");
        if (!context) return;
        context.setTransform(ratio, 0, 0, ratio, 0, 0);

        setSize({ width: viewport.width, height: viewport.height });
        const renderTask = rendered.render({ canvas, canvasContext: context, viewport });
        task = renderTask;
        await renderTask.promise;
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const message =
          cause instanceof Error && /Missing PDF|404|UnexpectedResponse/i.test(cause.message)
            ? "That document has not been fetched into data/ yet — run the ingestion CLI."
            : "The PDF could not be rendered in this browser.";
        setFailure({ token: `${doc}:${page}:${scale}`, message });
        onStatus?.({ phase: "failed", message });
      });

    return () => {
      cancelled = true;
      task?.cancel();
    };
    // `onStatus` is in the deps rather than behind a ref: a ref written during
    // render is what `react-hooks/refs` rejects, and the only caller passes a
    // `useState` setter, which is stable.
  }, [doc, page, scale, onStatus]);

  if (failed) {
    return (
      <div className="mx-auto max-w-[560px] rounded-[3px] border bg-background p-5">
        <p className="text-[12px] leading-[18px] text-muted-foreground">{failed}</p>
      </div>
    );
  }

  return (
    <div
      className="relative mx-auto bg-background shadow-sm"
      style={size ? { width: size.width, height: size.height } : undefined}
    >
      <canvas
        ref={canvasRef}
        className="block"
        style={size ? { width: size.width, height: size.height } : undefined}
      />
      {bbox && size ? (
        <div
          aria-hidden
          className="pointer-events-none absolute rounded-[2px] bg-source/25 ring-2 ring-source"
          style={{
            left: bbox[0] * scale,
            top: bbox[1] * scale,
            width: (bbox[2] - bbox[0]) * scale,
            height: (bbox[3] - bbox[1]) * scale,
          }}
        />
      ) : null}
    </div>
  );
}
