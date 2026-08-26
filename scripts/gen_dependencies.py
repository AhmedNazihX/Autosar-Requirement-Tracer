#!/usr/bin/env python3
"""Regenerate dependencies.txt from the real resolved state of both projects.

dependencies.txt is a generated inventory, not a source of truth. The
authoritative manifests stay where the tooling expects them:

    backend/pyproject.toml   + backend/uv.lock
    frontend/package.json    + frontend/package-lock.json

Writing the inventory by hand would guarantee it drifts the first time
anyone runs `uv add` or `npm install`, so it is derived instead. Run
`make deps` (or this script) after any dependency change and commit the
result.

Usage: python3 scripts/gen_dependencies.py [--check]

    --check  exit non-zero if dependencies.txt is stale, printing nothing
             else. Suitable for CI or a pre-commit hook.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
OUT = ROOT / "dependencies.txt"

# Why each frontend package is present. Several were never chosen directly:
# the shadcn CLI installs them as the peer dependencies of the components the
# approved design uses. Recording that here is the point of this file — a
# reviewer can see nothing arrived unexplained.
NODE_PROVENANCE = {
    "next": "framework (create-next-app)",
    "react": "framework (create-next-app)",
    "react-dom": "framework (create-next-app)",
    "typescript": "framework (create-next-app)",
    "eslint": "framework (create-next-app)",
    "eslint-config-next": "framework (create-next-app)",
    "@types/node": "framework (create-next-app)",
    "@types/react": "framework (create-next-app)",
    "@types/react-dom": "framework (create-next-app)",
    "tailwindcss": "styling (create-next-app, spec §11)",
    "@tailwindcss/postcss": "styling (create-next-app, spec §11)",
    "shadcn": "component CLI (spec §11)",
    "@base-ui/react": "shadcn primitive dependency",
    "class-variance-authority": "shadcn primitive dependency",
    "clsx": "shadcn primitive dependency",
    "tailwind-merge": "shadcn primitive dependency",
    "tw-animate-css": "shadcn primitive dependency",
    "lucide-react": "shadcn icon set",
    "next-themes": "shadcn dark-mode provider",
    "react-resizable-panels": "shadcn resizable panes (spec §7 split window)",
    "sonner": "shadcn toaster (story S5.2.5 error toasts)",
    "shiki": "code syntax highlighting (named in spec §7)",
    "react-markdown": "assistant message rendering (owner-approved)",
    "remark-gfm": "assistant message rendering (owner-approved)",
}

PY_PROVENANCE = {
    "fastapi": "backend framework (spec §3)",
    "uvicorn": "ASGI server (spec §3)",
    "pydantic": "the Requirement/CodeUnit models (spec §2)",
    "pydantic-settings": "environment configuration",
    "pyyaml": "project.yaml manifest loading",
    "pymupdf": "PDF parsing: text, pages, bboxes (spec §3)",
    "certifi": "TLS trust store for the corpus fetcher",
    "openai": "OpenAI-compatible SDK pointed at OpenRouter (spec §3)",
    "langchain": "the locked agent framework (spec §3); create_agent runs the tool-calling loop",
    "langchain-openai": "ChatOpenAI pointed at OpenRouter — the one chat client (WP2/WP3)",
    "chromadb": "vector store, one collection per project (spec §3)",
    "rank-bm25": "in-memory BM25, built at API startup (spec §3, §4)",
    "httpx": "test client / HTTP",
    "pytest": "tests",
    "pytest-asyncio": "async tests",
    "ruff": "lint",
}


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def python_section() -> list[str]:
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    project = data["project"]
    groups = data.get("dependency-groups") or {}

    # Resolved versions come from the venv, so they are what actually runs.
    resolved = json.loads(
        run(
            [
                "uv",
                "run",
                "python",
                "-c",
                "import json,importlib.metadata as m;"
                "print(json.dumps(sorted(({d.metadata['Name']:d.version} "
                "for d in m.distributions()), key=lambda x: list(x)[0].lower())))",
            ],
            BACKEND,
        )
    )
    flat = {}
    for entry in resolved:
        flat.update(entry)

    lines = [
        "BACKEND — Python",
        "-" * 72,
        f"interpreter: {project.get('requires-python', 'unspecified')}"
        f"  (pinned in backend/.python-version)",
        "manager:     uv  (backend/pyproject.toml + backend/uv.lock)",
        "",
        "declared, runtime:",
    ]
    for spec in project.get("dependencies", []):
        name = _pkg_name(spec)
        lines.append(_dep_line(name, spec, flat, PY_PROVENANCE))
    for group, items in groups.items():
        lines += ["", f"declared, {group}:"]
        for spec in items:
            lines.append(_dep_line(_pkg_name(spec), spec, flat, PY_PROVENANCE))

    declared = {
        _normalize(_pkg_name(s))
        for s in project.get("dependencies", [])
        + [i for items in groups.values() for i in items]
    }
    transitive = sorted(
        (n, v) for n, v in flat.items() if _normalize(n) not in declared
    )
    lines += [
        "",
        f"resolved transitively ({len(transitive)}):",
    ]
    lines += [f"  {n:<34} {v}" for n, v in transitive]
    lines += ["", f"total installed: {len(flat)} packages"]
    return lines


def node_section() -> list[str]:
    pkg = json.loads((FRONTEND / "package.json").read_text())
    lock = json.loads((FRONTEND / "package-lock.json").read_text())
    packages = lock.get("packages", {})

    def resolved_version(name: str) -> str:
        entry = packages.get(f"node_modules/{name}")
        return entry.get("version", "?") if entry else "?"

    lines = [
        "",
        "FRONTEND — Node",
        "-" * 72,
        "manager:     npm  (frontend/package.json + frontend/package-lock.json)",
        f"lockfile:    version {lock.get('lockfileVersion')}",
        "",
        "declared, runtime:",
    ]
    for name, spec in sorted(pkg.get("dependencies", {}).items()):
        why = NODE_PROVENANCE.get(name, "")
        lines.append(
            f"  {name:<28} {spec:<12} -> {resolved_version(name):<12}"
            + (f"  # {why}" if why else "")
        )
    lines += ["", "declared, dev:"]
    for name, spec in sorted(pkg.get("devDependencies", {}).items()):
        why = NODE_PROVENANCE.get(name, "")
        lines.append(
            f"  {name:<28} {spec:<12} -> {resolved_version(name):<12}"
            + (f"  # {why}" if why else "")
        )

    declared = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
    transitive = sorted(
        (k[len("node_modules/") :], v.get("version", "?"))
        for k, v in packages.items()
        if k.startswith("node_modules/")
        and k[len("node_modules/") :] not in declared
    )
    lines += [
        "",
        f"resolved transitively ({len(transitive)}) — the full tree is in "
        "package-lock.json;",
        "listed here so this inventory is complete rather than indicative:",
    ]
    lines += [f"  {n:<44} {v}" for n, v in transitive]
    lines += ["", f"total resolved: {len(declared) + len(transitive)} packages"]
    return lines


def _normalize(name: str) -> str:
    """PEP 503 normalization, so pyyaml and PyYAML are the same package."""
    return name.lower().replace("_", "-").replace(".", "-")


def _lookup(name: str, flat: dict) -> str:
    target = _normalize(name)
    for dist, version in flat.items():
        if _normalize(dist) == target:
            return version
    return "?"


def _pkg_name(spec: str) -> str:
    for sep in (">=", "==", "<=", "~=", ">", "<", "[", ";"):
        spec = spec.split(sep)[0]
    return spec.strip()


def _dep_line(name: str, spec: str, flat: dict, why_map: dict) -> str:
    # Distribution names are compared case-insensitively and treat - and _ as
    # equivalent (PEP 503), so `pyyaml` must find the `PyYAML` distribution.
    resolved = _lookup(name, flat)
    why = why_map.get(name.lower(), "")
    return (
        f"  {name:<22} {spec:<24} -> {resolved:<12}"
        + (f"  # {why}" if why else "")
    )


def render() -> str:
    header = [
        "ReqTrace — dependency inventory",
        "=" * 72,
        "",
        "GENERATED FILE — do not edit by hand.",
        "Regenerate with `make deps` (or `python3 scripts/gen_dependencies.py`)",
        "after any dependency change, and commit the result.",
        "",
        "The authoritative manifests are, and remain:",
        "    backend/pyproject.toml   + backend/uv.lock",
        "    frontend/package.json    + frontend/package-lock.json",
        "This file exists to show the whole picture in one place, with a note on",
        "why each direct dependency is present — several frontend packages were",
        "never chosen directly but installed by the shadcn CLI as peers of the",
        "components the approved design uses.",
        "",
        "No local ML runtimes: all LLM, embedding and rerank calls go through",
        "OpenRouter (spec §3). No torch and no sentence-transformers.",
        "onnxruntime does appear, transitively under chromadb; it backs",
        "Chroma's default embedding function, which this project never uses.",
        "Every collection is created with that function pinned off and every",
        "upsert passes explicit embeddings — see backend/retrieval/",
        "vector_store.py, which documents why the pin is required rather than",
        "optional.",
        "",
    ]
    body = python_section() + node_section()
    return "\n".join(header + body) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print(
                "dependencies.txt is stale — run `make deps` and commit the result.",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
