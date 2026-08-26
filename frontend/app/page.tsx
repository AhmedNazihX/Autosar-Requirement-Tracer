import { highlightCodeFixture } from "@/lib/code-highlight";
import { AppShell } from "@/components/reqtrace/app-shell";

/**
 * A Server Component, so Shiki's C grammar and its regex engine stay on the
 * server: what crosses to the browser is a plain token array. The shell itself
 * is a Client Component — it owns thread state and the SSE stream.
 */
export default async function Home() {
  const codeFile = await highlightCodeFixture();
  return <AppShell codeFile={codeFile} />;
}
