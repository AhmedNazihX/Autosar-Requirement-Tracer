# ReqTrace — frontend

The chat UI for ReqTrace (see the repository root `README.md` for the whole
project): a Next.js App Router app (TypeScript, Tailwind + shadcn/ui) with a
streaming chat transcript, structured citation chips, a split source pane
(PDF page render / annotated code view), thread history with checkpoint
restore, and a traceability-report drawer.

## Running it

From the repository root, `make dev` boots backend (:8000) and frontend
(:3000) together. Frontend alone:

```bash
npm install
npm run dev     # next dev on :3000
npm run build   # production build
npm run lint    # eslint
```

Typecheck (CI runs this; `next typegen` must come first because
`next-env.d.ts` references generated route types):

```bash
npx next typegen && npx tsc --noEmit
```

## The two rules that shape this app

**The backend is reached only through the Next.js proxy.** `next.config.ts`
rewrites `/api/py/:path*` to `localhost:8000`; there is no CORS
configuration and no direct backend URL anywhere in app code.

**The wire formats live here, authoritatively.** `lib/events.ts` defines the
chat SSE contract (`api/chat_events.py` is the Python mirror) and
`lib/reports.ts` the report contract (`api/reports.py`); do not fork them.
Citations are structured events, never parsed out of prose, and thread replay
rebuilds all UI state (tool chips, citations, cost lines, checkpoints) from
the stored events.

## Canned demo mode

```bash
NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=canned npm run dev
```

replays a committed fixture conversation with no backend, no index and no API
key. `lib/chat-sources.ts::chatSourceKind()` selects the world and
`lib/threads.ts` follows it (threads live in `localStorage` in canned mode);
anything that fetches must be gated on `chatSourceKind() === "live"` or the
demo fills with error panels.

## Working in this codebase

Read `AGENTS.md` first: this Next.js/React version is newer than most
tooling's training data — React's compiler rules reject setting state in an
effect body or writing a ref during render (derive or key instead).
