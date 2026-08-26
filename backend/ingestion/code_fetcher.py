"""Fetch the permitted C repository at its pinned SHA into ``data/repo/``.

Story S1.4.1. The repository URL, the pinned commit and the include/exclude
globs are *never* hard-coded here — they come from ``manifest.code`` (see
``core/manifest.py``), the corpus abstraction boundary the design spec (§2)
locks down.

Behaviour that matters downstream:

* **Skip-if-present, with zero git invocations.** A warm ``data/repo`` whose
  ``HEAD`` already resolves to the pinned SHA is reported ``skipped`` without
  running ``git`` at all. HEAD is resolved by *reading* ``.git`` (see
  :func:`read_head_sha`) rather than by shelling out, which is the directory
  equivalent of the PDF fetcher's ``.sha256`` sidecar check: a warm run
  touches nothing. Existence of the directory is never the test — the SHA is.
* **Loud SHA verification.** After any fetch/checkout the *real*
  ``git rev-parse HEAD`` is consulted and compared against the manifest. The
  verdict cache is keyed on ``git_sha`` (spec §5), so silently serving code
  from a different commit would serve stale verdicts for code that no longer
  exists — a correctness bug, not an inconvenience.
* **Shallow, single-commit fetch.** ``git init`` + ``git remote add`` +
  ``git fetch --depth 1 origin <sha>`` + ``git checkout --detach FETCH_HEAD``.
  Only the pinned commit is transferred; the repository's history is not.
* **Progress hook.** ``on_progress(step, done, total)`` mirrors the PDF
  fetcher's three-argument shape so story S3.6.2 can stream either over SSE.
  Nothing here knows about SSE.

Unlike the PDF fetcher there is no ``.part`` staging directory: a work tree is
not a single file, and copying 23 MB to rename it buys nothing here. The same
guarantee is obtained differently — ``git init`` leaves ``HEAD`` pointing at an
unborn branch, so an interrupted run cannot resolve to the pinned SHA and is
retried; a completed checkout is then confirmed by ``git rev-parse`` *and* by
the selected-file count being non-empty.

``git`` is invoked through the injected ``runner`` parameter so tests can
substitute a fake and *prove* the second run never calls it.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.manifest import CodeConfig, ProjectManifest

#: ``(step, done, total_or_None)`` — the same shape as the PDF fetcher's hook.
ProgressCallback = Callable[[str, int, int | None], None]

#: ``(git_args, cwd) -> stdout``. Must raise :class:`CodeFetchError` on failure.
GitRunner = Callable[[Sequence[str], Path], str]

FetchAction = Literal["cloned", "skipped", "rechecked_out"]

_TIMEOUT_SECONDS = 600
_HEX = "0123456789abcdef"

#: Progress steps reported for a cold fetch, in order.
_COLD_STEPS = ("init", "remote", "fetch", "checkout", "verify", "scan")


class CodeFetchError(Exception):
    """Raised when the repository cannot be fetched, or is at the wrong SHA."""


@dataclass(frozen=True)
class RepoFetchResult:
    """Outcome of ensuring the pinned repository snapshot is on disk.

    ``action`` is the assertable part: a second run over an intact
    ``data/repo`` must report ``skipped``.
    """

    action: FetchAction
    sha: str
    file_count: int
    path: Path


# --------------------------------------------------------------------------
# glob matching
# --------------------------------------------------------------------------
#
# Neither ``fnmatch`` nor 3.12's ``PurePath.match`` can do this job:
# ``fnmatch`` lets ``*`` cross ``/`` (so ``**/*.c`` would match nothing
# specific and ``communication/*/x.c`` would match any depth), and
# ``PurePath.match`` treats ``**`` as a plain ``*``. ``glob.translate`` would
# be the right tool but arrived in 3.13. So the manifest's patterns are
# translated here, in one tested function.


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a git-style path glob against ``/``-separated relative paths.

    ``**/`` matches zero or more whole path segments (so
    ``communication/CanIf/**/*.[ch]`` matches ``communication/CanIf/CanIf.c``
    as well as ``communication/CanIf/src/CanIf.c``); a trailing ``**`` matches
    everything below that point; ``*`` and ``?`` never cross ``/``; ``[...]``
    is a character class, with a leading ``!`` meaning negation.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(r"[^/]")
            i += 1
        elif pattern[i] == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape("["))
                i += 1
            else:
                body = pattern[i + 1 : close]
                body = ("^" + body[1:]) if body.startswith("!") else body
                out.append("[" + body + "]")
                i = close + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches_any(rel_path: str, patterns: Iterable[str]) -> bool:
    """True if ``rel_path`` matches any of ``patterns`` (see :func:`glob_to_regex`)."""
    return any(glob_to_regex(p).match(rel_path) is not None for p in patterns)


def select_files(root: Path, code: CodeConfig) -> list[Path]:
    """Files under ``root`` that the manifest's globs select, sorted.

    ``include_globs`` decides what enters the index and ``exclude_globs``
    removes from it — for this corpus that drops ``**/lwip-2.0.3/**`` (439
    vendored files that would otherwise dominate the index) and
    ``**/*_Cfg.[ch]`` (generated configuration). ``.git`` is never walked.

    The result is sorted so that two runs over the same snapshot index the
    same files in the same order.
    """
    selected: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        if not path_matches_any(rel, code.include_globs):
            continue
        if path_matches_any(rel, code.exclude_globs):
            continue
        selected.append(path)
    return selected


# --------------------------------------------------------------------------
# HEAD, read straight off the disk
# --------------------------------------------------------------------------


def _git_dir(repo_dir: Path) -> Path | None:
    """Resolve ``repo_dir``'s git directory, honouring a ``.git`` *file*."""
    dot_git = repo_dir / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        for line in text.splitlines():
            if line.startswith("gitdir:"):
                target = Path(line.split(":", 1)[1].strip())
                return target if target.is_absolute() else (repo_dir / target)
    return None


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in _HEX for c in value.lower())


def read_head_sha(repo_dir: Path) -> str | None:
    """The commit ``repo_dir``'s HEAD points at, read without invoking git.

    Returns ``None`` — never raises — when there is no git directory, when
    HEAD names a ref that does not exist yet (which is exactly the state
    ``git init`` leaves behind, so an interrupted fetch is never mistaken for
    a completed one), or when anything is unreadable. ``None`` means "cannot
    prove the snapshot is the pinned one", and the caller then re-fetches.
    """
    git_dir = _git_dir(repo_dir)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None

    if _is_sha(head):
        return head.lower()
    if not head.startswith("ref:"):
        return None

    ref = head.split(":", 1)[1].strip()
    try:
        loose = (git_dir / ref).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        loose = ""
    if _is_sha(loose):
        return loose.lower()

    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in packed.splitlines():
        if line.startswith(("#", "^")):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
            return parts[0].lower()
    return None


# --------------------------------------------------------------------------
# the git runner
# --------------------------------------------------------------------------


def subprocess_git_runner(args: Sequence[str], cwd: Path) -> str:
    """Run ``git <args>`` in ``cwd``; return stdout, raise on a non-zero exit.

    ``subprocess`` from the stdlib deliberately: a shallow one-commit fetch
    driven from a CLI does not justify a Python git binding (ruling R10).
    """
    command = ["git", *args]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodeFetchError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeFetchError(
            f"`{' '.join(command)}` timed out after {_TIMEOUT_SECONDS}s in {cwd}"
        ) from exc
    if completed.returncode != 0:
        raise CodeFetchError(
            f"`{' '.join(command)}` failed with exit {completed.returncode} in {cwd}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return completed.stdout


def repo_dir(manifest: ProjectManifest) -> Path:
    """Return the ``data/repo/`` directory for ``manifest``.

    Resolved against the repo root that owns the manifest, not the process
    CWD, so ``uv run`` from ``backend/`` and from the repo root agree.
    """
    root = manifest.project_root
    if root is None:
        raise CodeFetchError(
            "manifest has no project_root (was it constructed directly instead of "
            "via load_manifest?) — pass dest_dir explicitly"
        )
    return root / "data" / "repo"


def _ensure_remote(runner: GitRunner, dest: Path, url: str, *, fresh: bool) -> None:
    if fresh:
        runner(["remote", "add", "origin", url], dest)
        return
    try:
        current = runner(["remote", "get-url", "origin"], dest).strip()
    except CodeFetchError:
        runner(["remote", "add", "origin", url], dest)
        return
    if current != url:
        runner(["remote", "set-url", "origin", url], dest)


def fetch_repo(
    manifest: ProjectManifest,
    dest_dir: Path | None = None,
    *,
    runner: GitRunner = subprocess_git_runner,
    on_progress: ProgressCallback | None = None,
    force: bool = False,
) -> RepoFetchResult:
    """Ensure the manifest's pinned snapshot is checked out; report what happened.

    Runs **no git command at all** when ``data/repo`` already resolves to
    ``manifest.code.git_sha`` and the globs still select at least one file
    (unless ``force``); that case returns ``action="skipped"``.

    Raises :class:`CodeFetchError` if the checked-out SHA does not equal the
    manifest's after a fetch, or if the globs select no file at all.
    """
    code = manifest.code
    dest = dest_dir if dest_dir is not None else repo_dir(manifest)
    target_sha = code.git_sha.lower()
    dest.mkdir(parents=True, exist_ok=True)

    def progress(step: str) -> None:
        if on_progress is not None:
            on_progress(step, _COLD_STEPS.index(step) + 1, len(_COLD_STEPS))

    # Always read HEAD: `force` decides whether an intact snapshot may be
    # skipped, not whether a pre-existing snapshot is reported as a clone.
    on_disk = read_head_sha(dest)
    if on_disk == target_sha and not force:
        progress("scan")
        files = select_files(dest, code)
        if files:
            return RepoFetchResult(
                action="skipped", sha=target_sha, file_count=len(files), path=dest
            )
        # HEAD is right but the work tree is not there — check it out again
        # rather than reporting a snapshot that has no files in it.
    action: FetchAction = "rechecked_out" if on_disk is not None else "cloned"

    fresh = _git_dir(dest) is None
    progress("init")
    runner(["init", "--quiet"], dest)
    progress("remote")
    _ensure_remote(runner, dest, code.repo_url, fresh=fresh)
    progress("fetch")
    runner(["fetch", "--depth", "1", "--no-tags", "origin", target_sha], dest)
    progress("checkout")
    runner(["checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], dest)

    progress("verify")
    actual = runner(["rev-parse", "HEAD"], dest).strip().lower()
    if actual != target_sha:
        raise CodeFetchError(
            f"{dest}: checked-out commit {actual!r} does not match the manifest's pinned "
            f"code.git_sha {target_sha!r}. Refusing to index it — the verdict cache is "
            "keyed on git_sha, so indexing a different commit under the pinned SHA would "
            "serve verdicts about code that is not in the snapshot."
        )

    progress("scan")
    files = select_files(dest, code)
    if not files:
        raise CodeFetchError(
            f"{dest}: the snapshot is at the pinned SHA {target_sha} but "
            f"code.include_globs selected no files ({', '.join(code.include_globs)}). "
            "Either the globs no longer match this commit's layout or the checkout is "
            "incomplete."
        )
    return RepoFetchResult(action=action, sha=actual, file_count=len(files), path=dest)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``uv run python -m ingestion.code_fetcher <manifest.yaml> [--force]``."""
    import argparse

    from core.manifest import load_manifest

    parser = argparse.ArgumentParser(
        description="Fetch the permitted C repository at the manifest's pinned SHA."
    )
    parser.add_argument("manifest", help="path to projects/<name>/project.yaml")
    parser.add_argument("--dest", default=None, help="override the destination directory")
    parser.add_argument("--force", action="store_true", help="re-fetch even if present")
    parser.add_argument("--quiet", action="store_true", help="suppress progress lines")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    dest = Path(args.dest).resolve() if args.dest else repo_dir(manifest)
    print(f"destination: {dest}")
    print(f"repository:  {manifest.code.repo_url}")
    print(f"pinned sha:  {manifest.code.git_sha}  (license {manifest.code.license})")

    def progress(step: str, done: int, total: int | None) -> None:
        if not args.quiet:
            print(f"  [{done}/{total}] {step} ...")

    result = fetch_repo(manifest, dest, on_progress=progress, force=args.force)
    print(f"{result.action:<14} sha={result.sha}  {result.file_count} file(s) in scope")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
