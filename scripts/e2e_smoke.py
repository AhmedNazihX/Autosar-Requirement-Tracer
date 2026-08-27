#!/usr/bin/env python3
"""End-to-end smoke test: boot the real app and use it (story S7.1.1).

    python3 scripts/e2e_smoke.py            # boots what it needs, tears it down
    python3 scripts/e2e_smoke.py --keep     # leave servers running afterwards

Everything else in this repository tests a part. This tests the *product*: two
real servers, the Next.js proxy in front of the API, real models, the pinned
corpus, and a traceability report run to completion. It exits 0 only if a
reviewer following the README would have seen it work.

**It goes through the frontend proxy on purpose.** ``frontend/lib/chat-sources
.ts`` posts to ``/api/py/chat`` on port 3000 and ``next.config.ts`` rewrites
that to uvicorn; talking to port 8000 directly would skip the one piece of
wiring a fresh clone is most likely to get wrong, and CLAUDE.md makes that
proxy the only route the frontend may take.

**It reuses servers it finds already running**, and then leaves them alone.
Killing a developer's ``make dev`` to run a smoke test — or worse, leaving
orphaned servers holding 3000 and 8000 afterwards — makes a working app look
broken. What this process starts, this process stops; what it found, it leaves.

**It spends money**, a few cents: three real questions and a small scoped
report. The report is capped with ``limit`` and does not re-judge, so a second
run costs almost nothing — the verdict cache answers it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND, FRONTEND = ROOT / "backend", ROOT / "frontend"

API = "http://localhost:8000"
APP = "http://localhost:3000"
#: The proxy route, which is the only one the frontend is allowed to use.
CHAT = f"{APP}/api/py/chat"

#: How long each server gets to come up. `next dev` compiles on first request,
#: so it is given considerably longer than uvicorn.
API_BOOT_SECONDS = 60
APP_BOOT_SECONDS = 180

#: How long a scoped report may take before this gives up on it.
REPORT_SECONDS = 300

#: Mirrors ``RunStatus`` in ``backend/api/reports.py`` — a run is over when it
#: reaches any of these. Spelled out rather than guessed: the first version of
#: this script polled for "done", which no run ever reports, so a report that
#: had actually succeeded in milliseconds was declared a 300-second timeout.
TERMINAL = ("succeeded", "failed", "aborted", "cancelled")
SUCCEEDED = "succeeded"

#: The report scope. Small and cached-on-rerun by design — this proves the job
#: completes, not that the corpus is fully judged. `CanIf` rather than `Can`:
#: the permitted repository contains no CAN Driver implementation at all
#: (finding A1), so a `Can` scope can only ever produce `missing`.
REPORT_SCOPE = {"module": "CanIf", "limit": 3}

#: Every thread this run creates, so they can be deleted at the end. A smoke
#: test that leaves a dozen "How does the CAN Interface..." threads in the
#: sidebar has made the app worse to look at, which is a strange thing for a
#: quality check to do.
CREATED_THREADS: list[str] = []

#: Prefix for this run's threads. **A fresh id per run matters.** The first
#: version reused `smoke-0/1/2`, so by the third run the thread already held
#: the same question and its answer — and the agent, quite correctly, answered
#: from that history without calling a tool again. The script read that as an
#: ungrounded answer and failed a product that was behaving properly.
RUN_ID = uuid.uuid4().hex[:8]

#: Three questions, each exercising a different path to an answer.
QUESTIONS = [
    {
        "ask": "What does SWS_CANIF_00023 require?",
        "why": "an exact id — must go to lookup_requirement, never a search",
        "expect_tool": "lookup_requirement",
        "expect_citations": True,
    },
    {
        "ask": "How does the CAN Interface report a bus-off condition?",
        "why": "a behaviour — must go through the full retrieval pipeline",
        "expect_tool": "search_requirements",
        "expect_citations": True,
    },
    {
        "ask": "Is SWS_CANIF_00661 implemented in the pinned snapshot?",
        "why": "evidence — must reach the judge and return a real verdict",
        "expect_tool": "check_implementation",
        "expect_citations": True,
    },
]


class SmokeFailure(Exception):
    """A check that did not pass. Carries the sentence a reader needs."""


@dataclass
class Servers:
    """The servers this process started, and only those."""

    started: list[subprocess.Popen] = field(default_factory=list)

    def stop(self) -> None:
        for process in reversed(self.started):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def get(url: str, timeout: float = 30) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(url: str, payload: dict, timeout: float = 300) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def sse_events(body: str) -> list[dict]:
    """Parse an SSE body the way ``chat-sources.ts`` parses it."""
    events = []
    for frame in body.split("\n\n"):
        data = "\n".join(
            line[len("data:") :].strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        )
        if data:
            events.append(json.loads(data))
    return events


def alive(url: str) -> bool:
    try:
        return get(url, timeout=3)[0] == 200
    except Exception:  # noqa: BLE001 - "not up yet" is every exception here
        return False


def wait_until(url: str, seconds: int, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if alive(url):
            return
        time.sleep(1)
    raise SmokeFailure(f"{what} did not come up within {seconds}s at {url}")


def step(message: str) -> None:
    print(f"\n\033[1m→ {message}\033[0m", flush=True)


def ok(message: str) -> None:
    print(f"  \033[32m✓\033[0m {message}", flush=True)


# --------------------------------------------------------------------------
# booting
# --------------------------------------------------------------------------


def boot(servers: Servers) -> None:
    """Start whichever of the two servers is not already answering."""
    if alive(f"{API}/health"):
        ok("backend already running on :8000 — reusing it, and leaving it up")
    else:
        uvicorn = BACKEND / ".venv" / "bin" / "uvicorn"
        if not uvicorn.is_file():
            raise SmokeFailure(
                f"no virtualenv at {uvicorn} — run 'make install' before this script"
            )
        servers.started.append(
            subprocess.Popen(
                [str(uvicorn), "api.main:app", "--port", "8000"],
                cwd=BACKEND,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        wait_until(f"{API}/health", API_BOOT_SECONDS, "the backend")
        ok("backend started on :8000")

    if alive(APP):
        ok("frontend already running on :3000 — reusing it, and leaving it up")
    else:
        if not (FRONTEND / "node_modules").is_dir():
            raise SmokeFailure(
                "frontend/node_modules is missing — run 'make install' before this script"
            )
        servers.started.append(
            subprocess.Popen(
                ["npm", "run", "dev"],
                cwd=FRONTEND,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        wait_until(APP, APP_BOOT_SECONDS, "the frontend")
        ok("frontend started on :3000")


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def check_ready() -> None:
    """The corpus must be indexed, or every answer below is meaningless."""
    status, body = get(f"{API}/setup/status")
    if status != 200:
        raise SmokeFailure(f"GET /setup/status answered {status}")
    if not body.get("ready"):
        raise SmokeFailure(
            "the corpus is not indexed — run the ingestion CLI first:\n"
            "    cd backend && uv run python -m ingestion.run "
            "../projects/autosar-can/project.yaml"
        )
    ok(f"corpus ready: {body.get('requirements')} requirements, "
       f"{body.get('code_units')} code units")


def check_proxy() -> None:
    """The rewrite must work, because it is the frontend's only route in."""
    status, body = get(f"{APP}/api/py/health")
    if status != 200 or not isinstance(body, dict) or body.get("status") != "ok":
        raise SmokeFailure(
            f"the Next.js proxy did not reach the backend: /api/py/health gave "
            f"{status} {body!r}. Check frontend/next.config.ts rewrites()."
        )
    ok("the /api/py proxy reaches the backend")


def check_question(index: int, case: dict) -> float:
    """One real question, through the proxy, asserted on its event stream."""
    thread_id = f"smoke-{RUN_ID}-{index}"
    CREATED_THREADS.append(thread_id)
    status, body = post(CHAT, {"thread_id": thread_id, "message": case["ask"]})
    if status != 200:
        raise SmokeFailure(f"POST /chat answered {status} for {case['ask']!r}: {body[:200]}")

    events = sse_events(body)
    kinds = [event["type"] for event in events]
    if not kinds:
        raise SmokeFailure(f"no SSE events at all for {case['ask']!r}")

    # Every stream must terminate, or the reducer renders it as interrupted.
    if kinds[-1] not in ("done", "error"):
        raise SmokeFailure(f"the stream for {case['ask']!r} ended on {kinds[-1]!r}, not done/error")
    if kinds[-1] == "error":
        detail = next(e["data"].get("message") for e in events if e["type"] == "error")
        raise SmokeFailure(f"{case['ask']!r} failed: {detail}")

    tools = [e["data"].get("tool") for e in events if e["type"] == "tool_start"]
    if not tools:
        raise SmokeFailure(
            f"{case['ask']!r} was answered with no tool call at all. That is an "
            "answer from the model's own knowledge of AUTOSAR, which is the one "
            "failure this product exists to prevent."
        )
    if case["expect_tool"] not in tools:
        raise SmokeFailure(
            f"{case['ask']!r} should have called {case['expect_tool']} "
            f"({case['why']}) but called {tools}"
        )

    citations = [e for e in events if e["type"] == "citation"]
    if case["expect_citations"] and not citations:
        raise SmokeFailure(
            f"{case['ask']!r} produced no citation events — an uncited answer is "
            "the failure this product exists to prevent"
        )

    text = "".join(e["data"]["text"] for e in events if e["type"] == "token")
    if not text.strip():
        raise SmokeFailure(f"{case['ask']!r} streamed no text")

    usage = [e["data"] for e in events if e["type"] == "usage"]
    if len(usage) != 1:
        raise SmokeFailure(
            f"expected exactly one usage event for {case['ask']!r}, got {len(usage)} — "
            "the reducer keeps only the last one it sees"
        )

    spent = usage[0].get("cost_usd", 0.0)
    ok(f"{case['ask']}")
    print(f"      {case['expect_tool']} · {len(citations)} citation(s) · "
          f"{len(text)} chars · ${spent:.4f}")
    return spent


def check_report() -> float:
    """A scoped traceability report, launched and polled to completion."""
    status, body = post(f"{API}/reports", {"scope": REPORT_SCOPE})
    if status != 200:
        raise SmokeFailure(f"POST /reports answered {status}: {body[:300]}")
    launch = json.loads(body)
    run_id = launch["job_id"]
    print(f"      scope {launch['scope_label']} · {launch['requirements']} requirement(s) · "
          f"est ${launch.get('est_cost_usd') or 0:.4f} · ceiling ${launch['ceiling_usd']:.2f}")

    deadline = time.monotonic() + REPORT_SECONDS
    view = {}
    while time.monotonic() < deadline:
        code, view = get(f"{API}/reports/{run_id}")
        if code != 200:
            raise SmokeFailure(f"GET /reports/{run_id} answered {code}")
        if view["status"] in TERMINAL:
            break
        time.sleep(2)
    else:
        raise SmokeFailure(f"the report did not finish within {REPORT_SECONDS}s")

    if view["status"] != SUCCEEDED:
        raise SmokeFailure(
            f"the report ended as {view['status']}: {view.get('error') or 'no reason given'}"
        )

    rows = (view.get("result") or {}).get("rows") or []
    if not rows:
        raise SmokeFailure("the report finished with an empty matrix")
    statuses = sorted({row["status"] for row in rows})

    code, export = get(f"{API}/reports/{run_id}/export?fmt=md")
    if code != 200 or not str(export).strip():
        raise SmokeFailure(f"the Markdown export answered {code} or was empty")

    ok(f"report completed: {len(rows)} row(s), verdicts {statuses}, "
       f"${view['cost_usd']:.4f}")
    ok(f"Markdown export is {len(str(export))} chars")
    return view["cost_usd"]


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def delete(url: str) -> int:
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def cleanup_threads() -> None:
    """Remove the threads this run created. Best-effort and never fatal —
    a smoke test that passed must not fail on tidying up after itself."""
    removed = 0
    for thread_id in CREATED_THREADS:
        try:
            if delete(f"{API}/threads/{thread_id}") in (204, 404):
                removed += 1
        except Exception:  # noqa: BLE001 - tidying is not worth an exit code
            pass
    if removed:
        ok(f"removed {removed} smoke thread(s) from the sidebar")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the servers this script started running, and keep its threads",
    )
    args = parser.parse_args(argv)

    servers = Servers()
    spent = 0.0
    started = time.monotonic()
    try:
        step("Booting")
        boot(servers)

        step("Checking the app can answer at all")
        check_ready()
        check_proxy()

        step("Three questions, through the frontend proxy")
        for index, case in enumerate(QUESTIONS):
            spent += check_question(index, case)

        step("One scoped traceability report, to completion")
        spent += check_report()

    except SmokeFailure as failure:
        print(f"\n\033[31m✗ FAILED\033[0m  {failure}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n\033[33minterrupted\033[0m", file=sys.stderr)
        return 130
    except Exception as unexpected:  # noqa: BLE001 - a smoke test reports, never traces
        print(f"\n\033[31m✗ FAILED\033[0m  unexpected {type(unexpected).__name__}: "
              f"{unexpected}\n", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            cleanup_threads()
        if args.keep and servers.started:
            print("\n--keep: leaving the servers this script started running.")
        else:
            servers.stop()

    elapsed = time.monotonic() - started
    print(f"\n\033[32m✓ PASSED\033[0m  {len(QUESTIONS)} questions and 1 report in "
          f"{elapsed:.0f}s, ${spent:.4f} spent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
