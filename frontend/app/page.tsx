import { highlightCodeFixture } from "@/lib/code-highlight";
import { BootGate } from "@/components/reqtrace/boot-gate";

/**
 * A Server Component, so Shiki's C grammar and its regex engine stay on the
 * server: what crosses to the browser is a plain token array. The gate below
 * is a Client Component — it owns the readiness check, thread state and the
 * SSE stream.
 *
 * The highlighted fixture is still rendered here rather than fetched: it is
 * the code pane's content until a citation points somewhere real, and doing it
 * on the server keeps Shiki's grammar out of the bundle. Story S5.3.2 adds the
 * live `GET /code/{path}` fetch alongside it.
 */
export default async function Home() {
  const codeFile = await highlightCodeFixture();
  return <BootGate codeFile={codeFile} />;
}
