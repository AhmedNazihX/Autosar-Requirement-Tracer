"""Tests for ingestion/code_fetcher.py (S1.4.1).

The acceptance criterion is "pinned SHA checked out, re-run no-op", so git is
always a fake here and the tests assert on how many times it was invoked — a
second run that reports ``skipped`` while having shelled out to git again
would pass a naive assertion on the action alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from core.manifest import load_manifest
from ingestion.code_fetcher import (
    CodeFetchError,
    RepoFetchResult,
    fetch_repo,
    glob_to_regex,
    path_matches_any,
    read_head_sha,
    repo_dir,
    select_files,
    subprocess_git_runner,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"

PINNED_SHA = "09433770bebb8f27a7b480d7c96d814c68ffed3e"
OTHER_SHA = "1111111111111111111111111111111111111111"

#: What the fake checkout materializes: two in-scope files, one vendored file
#: and one generated-config file that the manifest's excludes must remove.
WORKTREE = {
    "communication/CanIf/src/CanIf.c": "/* @req 4.0.3/CANIF005 */\nvoid CanIf_Init(void) {}\n",
    "communication/CanIf/inc/CanIf.h": "void CanIf_Init(void);\n",
    "communication/CanTp/src/lwip-2.0.3/vendor.c": "int vendored(void) { return 0; }\n",
    "communication/CanIf/src/CanIf_Cfg.c": "int generated = 1;\n",
    "README.md": "not C, not in any include glob\n",
}


class FakeGit:
    """A fake ``git`` that records every invocation and fakes just enough state.

    ``init`` leaves HEAD on an unborn branch (as real git does), ``checkout``
    writes the detached HEAD and materializes the work tree, and ``rev-parse``
    reports whatever HEAD says — so ``checkout_sha`` can be pointed at the
    wrong commit to exercise the verification failure.
    """

    def __init__(self, *, checkout_sha: str = PINNED_SHA, worktree: dict | None = None) -> None:
        self.checkout_sha = checkout_sha
        self.worktree = WORKTREE if worktree is None else worktree
        self.calls: list[list[str]] = []
        self.remote_url: str | None = None

    @property
    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]

    def __call__(self, args: Sequence[str], cwd: Path) -> str:
        args = list(args)
        self.calls.append(args)
        git_dir = cwd / ".git"
        if args[0] == "init":
            git_dir.mkdir(parents=True, exist_ok=True)
            (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
            return ""
        if args[:2] == ["remote", "get-url"]:
            if self.remote_url is None:
                raise CodeFetchError("`git remote get-url origin` failed with exit 2")
            return self.remote_url + "\n"
        if args[:2] in (["remote", "add"], ["remote", "set-url"]):
            self.remote_url = args[3]
            return ""
        if args[0] == "fetch":
            return ""
        if args[0] == "checkout":
            (git_dir / "HEAD").write_text(self.checkout_sha + "\n", encoding="utf-8")
            for rel, body in self.worktree.items():
                path = cwd / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            return ""
        if args[0] == "rev-parse":
            return (git_dir / "HEAD").read_text(encoding="utf-8")
        raise AssertionError(f"unexpected git invocation: {args}")


@pytest.fixture
def manifest():
    return load_manifest(REAL_MANIFEST_PATH)


# --------------------------------------------------------------------------
# the acceptance criterion: pinned SHA checked out, re-run is a no-op
# --------------------------------------------------------------------------


def test_first_run_clones_and_checks_out_the_pinned_sha(manifest, tmp_path: Path):
    git = FakeGit()
    result = fetch_repo(manifest, tmp_path, runner=git)

    assert isinstance(result, RepoFetchResult)
    assert result.action == "cloned"
    assert result.sha == manifest.code.git_sha == PINNED_SHA
    assert result.path == tmp_path
    # the shallow single-commit pattern, in order
    assert git.verbs == ["init", "remote", "fetch", "checkout", "rev-parse"]
    assert git.calls[2] == ["fetch", "--depth", "1", "--no-tags", "origin", PINNED_SHA]
    assert git.remote_url == manifest.code.repo_url
    # only the two in-scope files count; lwip, *_Cfg.c and README are out
    assert result.file_count == 2


def test_second_run_is_a_no_op_with_zero_git_invocations(manifest, tmp_path: Path):
    first = FakeGit()
    fetch_repo(manifest, tmp_path, runner=first)
    assert first.calls  # the first run really did work

    second = FakeGit()
    result = fetch_repo(manifest, tmp_path, runner=second)

    assert result.action == "skipped"
    assert result.sha == PINNED_SHA
    assert result.file_count == 2
    assert second.calls == []


def test_force_refetches_an_intact_snapshot(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    git = FakeGit()
    git.remote_url = manifest.code.repo_url  # the first run already configured it
    result = fetch_repo(manifest, tmp_path, runner=git, force=True)

    assert result.action == "rechecked_out"
    assert git.verbs == ["init", "remote", "fetch", "checkout", "rev-parse"]
    # the remote already existed with the right url, so it is probed, not re-added
    assert git.calls[1] == ["remote", "get-url", "origin"]


def test_a_stale_remote_url_is_updated(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    git = FakeGit()
    git.remote_url = "https://example.invalid/moved.git"
    fetch_repo(manifest, tmp_path, runner=git, force=True)

    assert ["remote", "set-url", "origin", manifest.code.repo_url] in git.calls
    assert git.remote_url == manifest.code.repo_url


def test_a_missing_remote_on_an_existing_repo_is_added(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    git = FakeGit()  # remote_url is None: `git remote get-url origin` fails
    fetch_repo(manifest, tmp_path, runner=git, force=True)

    assert git.calls[1] == ["remote", "get-url", "origin"]
    assert git.calls[2] == ["remote", "add", "origin", manifest.code.repo_url]


def test_a_different_sha_on_disk_triggers_a_refetch(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    (tmp_path / ".git" / "HEAD").write_text(OTHER_SHA + "\n", encoding="utf-8")

    git = FakeGit()
    result = fetch_repo(manifest, tmp_path, runner=git)
    assert result.action == "rechecked_out"
    assert result.sha == PINNED_SHA
    assert "fetch" in git.verbs


def test_matching_head_with_an_empty_worktree_is_checked_out_again(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    for path in sorted(tmp_path.rglob("*.[ch]")):
        path.unlink()

    git = FakeGit()
    result = fetch_repo(manifest, tmp_path, runner=git)
    assert result.action == "rechecked_out"
    assert result.file_count == 2
    assert "checkout" in git.verbs


# --------------------------------------------------------------------------
# SHA verification
# --------------------------------------------------------------------------


def test_sha_mismatch_after_checkout_raises_with_a_clear_message(manifest, tmp_path: Path):
    git = FakeGit(checkout_sha=OTHER_SHA)
    with pytest.raises(CodeFetchError) as excinfo:
        fetch_repo(manifest, tmp_path, runner=git)

    message = str(excinfo.value)
    assert OTHER_SHA in message
    assert PINNED_SHA in message
    assert "git_sha" in message
    assert "verdict cache" in message


def test_pinned_sha_comes_from_the_manifest_not_from_python(manifest, tmp_path: Path):
    """Changing the manifest changes what is fetched — nothing is hard-coded."""
    manifest.code.git_sha = OTHER_SHA
    git = FakeGit(checkout_sha=OTHER_SHA)
    result = fetch_repo(manifest, tmp_path, runner=git)

    assert result.sha == OTHER_SHA
    assert git.calls[2] == ["fetch", "--depth", "1", "--no-tags", "origin", OTHER_SHA]


def test_globs_selecting_nothing_raises(manifest, tmp_path: Path):
    git = FakeGit(worktree={"README.md": "nothing in scope\n"})
    with pytest.raises(CodeFetchError) as excinfo:
        fetch_repo(manifest, tmp_path, runner=git)
    assert "selected no files" in str(excinfo.value)


# --------------------------------------------------------------------------
# glob filtering
# --------------------------------------------------------------------------


def test_select_files_includes_canif_and_excludes_lwip_and_cfg(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    selected = {p.relative_to(tmp_path).as_posix() for p in select_files(tmp_path, manifest.code)}

    assert "communication/CanIf/src/CanIf.c" in selected
    assert "communication/CanIf/inc/CanIf.h" in selected
    assert "communication/CanTp/src/lwip-2.0.3/vendor.c" not in selected
    assert "communication/CanIf/src/CanIf_Cfg.c" not in selected
    assert "README.md" not in selected


def test_select_files_never_walks_dot_git(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    stowaway = tmp_path / ".git" / "communication" / "CanIf" / "src" / "sneaky.c"
    stowaway.parent.mkdir(parents=True, exist_ok=True)
    stowaway.write_text("int x;\n", encoding="utf-8")

    selected = {p.relative_to(tmp_path).as_posix() for p in select_files(tmp_path, manifest.code)}
    assert not any(rel.startswith(".git/") for rel in selected)


def test_select_files_is_sorted_and_stable(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    first = select_files(tmp_path, manifest.code)
    assert first == sorted(first)
    assert first == select_files(tmp_path, manifest.code)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # `**/` spans zero or more whole segments
        ("communication/CanIf/**/*.[ch]", "communication/CanIf/src/CanIf.c", True),
        ("communication/CanIf/**/*.[ch]", "communication/CanIf/CanIf.h", True),
        ("communication/CanIf/**/*.[ch]", "communication/CanIf/a/b/c/CanIf.c", True),
        ("communication/CanIf/**/*.[ch]", "communication/CanIf/src/CanIf.cpp", False),
        ("communication/CanIf/**/*.[ch]", "communication/CanTp/src/CanTp.c", False),
        # a trailing `**` takes everything below, files included
        ("**/lwip-2.0.3/**", "communication/x/lwip-2.0.3/src/api/api_lib.c", True),
        ("**/lwip-2.0.3/**", "lwip-2.0.3/a.c", True),
        ("**/lwip-2.0.3/**", "communication/lwip-2.0.4/a.c", False),
        # `*` never crosses a separator
        ("**/*_Cfg.[ch]", "communication/CanIf/src/CanIf_Cfg.c", True),
        ("**/*_Cfg.[ch]", "communication/CanIf/src/CanIf_Cfg.h", True),
        ("**/*_Cfg.[ch]", "communication/CanIf/src/CanIf.c", False),
        ("communication/*/x.c", "communication/CanIf/src/x.c", False),
        # a match must cover the whole path
        ("communication/CanIf", "communication/CanIf/src/CanIf.c", False),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool):
    assert (glob_to_regex(pattern).match(path) is not None) is expected


def test_path_matches_any_is_a_disjunction():
    assert path_matches_any("communication/Com/src/Com.c", ["a/**", "communication/Com/**/*.[ch]"])
    assert not path_matches_any("communication/Com/src/Com.c", ["a/**", "b/**"])


# --------------------------------------------------------------------------
# HEAD resolution without invoking git
# --------------------------------------------------------------------------


def test_read_head_sha_detached(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(PINNED_SHA.upper() + "\n", encoding="utf-8")
    assert read_head_sha(tmp_path) == PINNED_SHA


def test_read_head_sha_follows_a_loose_ref(tmp_path: Path):
    ref = tmp_path / ".git" / "refs" / "heads"
    ref.mkdir(parents=True)
    (ref / "master").write_text(PINNED_SHA + "\n", encoding="utf-8")
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    assert read_head_sha(tmp_path) == PINNED_SHA


def test_read_head_sha_follows_packed_refs(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{PINNED_SHA} refs/heads/master\n",
        encoding="utf-8",
    )
    assert read_head_sha(tmp_path) == PINNED_SHA


def test_read_head_sha_on_an_unborn_branch_is_none(tmp_path: Path):
    """`git init` alone must never look like a completed checkout."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    assert read_head_sha(tmp_path) is None


def test_read_head_sha_honours_a_gitdir_file(tmp_path: Path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "HEAD").write_text(PINNED_SHA + "\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    assert read_head_sha(work) == PINNED_SHA


def test_read_head_sha_missing_repo_is_none(tmp_path: Path):
    assert read_head_sha(tmp_path) is None


def test_read_head_sha_garbage_is_none(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("not a sha and not a ref\n", encoding="utf-8")
    assert read_head_sha(tmp_path) is None


# --------------------------------------------------------------------------
# progress hook + wiring
# --------------------------------------------------------------------------


def test_on_progress_reports_steps(manifest, tmp_path: Path):
    seen: list[tuple[str, int, int | None]] = []
    fetch_repo(manifest, tmp_path, runner=FakeGit(), on_progress=lambda *a: seen.append(a))

    steps = [name for name, _, _ in seen]
    assert steps == ["init", "remote", "fetch", "checkout", "verify", "scan"]
    assert all(total == len(steps) for _, _, total in seen)
    assert [done for _, done, _ in seen] == [1, 2, 3, 4, 5, 6]


def test_on_progress_on_a_skipped_run_only_scans(manifest, tmp_path: Path):
    fetch_repo(manifest, tmp_path, runner=FakeGit())
    seen: list[str] = []
    fetch_repo(manifest, tmp_path, runner=FakeGit(), on_progress=lambda s, *_: seen.append(s))
    assert seen == ["scan"]


def test_repo_dir_is_repo_data_repo(manifest):
    assert repo_dir(manifest) == REPO_ROOT / "data" / "repo"


def test_repo_dir_without_project_root_raises(manifest):
    manifest.manifest_dir = None
    with pytest.raises(CodeFetchError) as excinfo:
        repo_dir(manifest)
    assert "project_root" in str(excinfo.value)


def test_subprocess_runner_reports_a_failing_git_command(tmp_path: Path):
    with pytest.raises(CodeFetchError) as excinfo:
        subprocess_git_runner(["rev-parse", "HEAD"], tmp_path)
    assert "failed with exit" in str(excinfo.value)


def test_subprocess_runner_returns_stdout(tmp_path: Path):
    out = subprocess_git_runner(["--version"], tmp_path)
    assert out.startswith("git version")
