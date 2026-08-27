/**
 * SSE client plumbing shared by every stream this app reads.
 *
 * Three streams — chat, report progress, the ingestion log — each grew a
 * private copy of the same two things: the byte loop that reassembles
 * blank-line-separated frames from a `ReadableStream`, and the `data:` line
 * parser that turns one frame into a JSON payload. The copies had already
 * diverged in shape (an `indexOf` loop vs `split`/`pop`) purely from being
 * retyped; the mechanics now live here, once. What stays with each stream is
 * the only thing that differs: its type guard and what it does with a frame.
 *
 * The Python mirror of this split is `api/sse.py`.
 */

/**
 * The blank-line-separated frames of an SSE body, one string per frame.
 *
 * A trailing partial frame stays buffered until its terminator arrives, so a
 * frame split across network chunks is never parsed half-read. Comment
 * frames (`: keep-alive`) come through like any other and fall out at the
 * parse step, which finds no `data:` lines in them.
 */
export async function* sseBlocks(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) yield block;
    }
  } finally {
    // Runs on normal end, on error, and when a consumer stops early (a
    // `return` inside `for await` closes the generator) — the lock must not
    // outlive the read. Only the chat copy of this loop remembered to do
    // this; the other two never released, which is drift of exactly the kind
    // one shared home ends.
    reader.releaseLock();
  }
}

/**
 * One frame's payload, parsed and checked, or `null`.
 *
 * Joins the frame's `data:` lines, JSON-parses them, and applies the caller's
 * type guard. Anything off-contract is dropped rather than rendered — the
 * event models are the contract; the wire does not get to widen them.
 */
export function parseSseFrame<T>(
  block: string,
  guard: (value: unknown) => value is T,
): T | null {
  const payload = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!payload) return null;
  try {
    const parsed: unknown = JSON.parse(payload);
    return guard(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
