# ReqTrace

ReqTrace is a requirements-to-code traceability chatbot for automotive
embedded software. It answers questions about AUTOSAR SWS requirements with
source-linked citations and a split-window PDF/code preview, checks a
permitted C repository for implementation evidence, and generates
SRS → SWS → code traceability reports.

## Quickstart

```
make install   # uv sync (backend) + npm install (frontend)
make dev       # boots backend (uvicorn :8000) and frontend (next dev :3000) together
```

The backend requires an `OPENROUTER_API_KEY` — copy `backend/.env.example` to
`backend/.env` and fill it in (see `CLAUDE.md` for details).

## Status

Not yet implemented. This is the project scaffold only: backend (FastAPI) and
frontend (Next.js) skeletons, dev wiring, and repo hygiene. See
`docs/specs/2026-08-26-reqtrace-design.md` for the approved design and
`.claude/plans/reqtrace-workplan.md` for the implementation plan.
