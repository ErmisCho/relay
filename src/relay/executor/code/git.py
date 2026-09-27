"""Worker-side git for code tasks: branch per task, commit, push that branch only.

The agent's shell has no network, so pushing happens here, in the worker. Two guards:

- :func:`push_branch` pushes exactly one ref, ``refs/heads/<branch>`` to the same name, never
  forced, and refuses anything that is not a ``relay/`` task branch or that is the default
  branch (``main``, ``master``, or whatever ``origin/HEAD`` names). Nothing here merges.
- The workspace (``.git`` included) is writable by the agent. Commits use Git plumbing with
  ``hash-object --no-filters`` and ``commit-tree``, so agent-controlled hooks, clean filters and
  signing programs never execute. Other commands disable hooks, fsmonitor and owner config.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from relay.executor.workspace import _slugify

BRANCH_PREFIX = "relay/"
PROTECTED_BRANCHES = frozenset({"main", "master"})
INITIAL_BRANCH = "main"
AUTHOR = ("relay", "relay@localhost")
# Written into .git/info/exclude: the sandbox points HOME and TMPDIR into the workspace, so
# tool caches and dotfiles land there and must not be committed.
EXCLUDES = (
    ".tmp/",
    ".cache/",
    ".config/",
    ".local/",
    ".npm/",
    ".gitconfig",
    ".pytest_cache/",
    "__pycache__/",
)
_HARDENING = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.ext.allow=never",
)
_IDENTITY = ("-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}")


class GitError(RuntimeError):
    pass


class ProtectedBranchError(GitError):
    """A push to a default/protected or non-task branch was refused."""


def branch_for(task_id: str, goal: str) -> str:
    """``relay/<first 8 hex chars of the task id>-<slug of the goal>``."""
    return f"{BRANCH_PREFIX}{task_id.replace('-', '')[:8]}-{_slugify(goal)[:30].rstrip('-')}"


def _env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    env.update(extra or {})
    return env


def git(
    folder: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    prefix: Sequence[str] = (),
    check: bool = True,
    strip: bool = True,
    input_text: str | None = None,
) -> str:
    """Run git in ``folder`` (under ``prefix``, e.g. a sandbox argv); stdout, stripped."""
    argv = [*prefix, "git", *_HARDENING, *args]
    proc = subprocess.run(
        argv,
        cwd=folder,
        env=dict(env) if env is not None else _env(),
        capture_output=True,
        text=True,
        input=input_text,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip() if strip else proc.stdout


def _ok(folder: Path, *args: str) -> bool:
    return (
        subprocess.run(
            ["git", *_HARDENING, *args], cwd=folder, env=_env(), capture_output=True, check=False
        ).returncode
        == 0
    )


def current_branch(folder: Path) -> str | None:
    out = git(folder, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return out or None


def default_branch(folder: Path) -> str:
    """``origin/HEAD``'s branch if known, else ``main`` / ``master`` if present, else ``main``."""
    ref = git(folder, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if ref.startswith("origin/"):
        return ref.removeprefix("origin/")
    for name in ("main", "master"):
        if _ok(folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return INITIAL_BRANCH


def origin_url(folder: Path) -> str | None:
    return git(folder, "config", "--get", "remote.origin.url", check=False) or None


def checkout(folder: Path, branch: str) -> dict[str, str]:
    """Make ``folder`` a repository on ``branch`` (created from the current branch); idempotent.

    A folder that is not a repository yet gets ``git init`` and an empty initial commit, so the
    task branch has a base to open a pull request against. Returns ``{"branch", "base"}``.
    """
    if not (folder / ".git").exists():
        git(folder, "init", "--quiet", "--initial-branch", INITIAL_BRANCH)
    if not _ok(folder, "rev-parse", "--verify", "--quiet", "HEAD"):
        git(folder, *_IDENTITY, "commit", "--quiet", "--allow-empty", "-m", "Initial commit")
    exclude = folder / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    present = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [e for e in EXCLUDES if e not in present]
    if missing:
        with exclude.open("a") as fh:
            fh.write("".join(f"{e}\n" for e in missing))
    here = current_branch(folder)
    base = here if here and not here.startswith(BRANCH_PREFIX) else default_branch(folder)
    if here != branch:
        if _ok(folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            git(folder, "checkout", "--quiet", branch)
        else:
            git(folder, "checkout", "--quiet", "-b", branch)
    return {"branch": branch, "base": base}


def commit_all(folder: Path, branch: str, message: str) -> str:
    """Commit the working tree without executing repository-controlled hooks or filters."""
    if current_branch(folder) != branch:
        raise GitError(f"refusing to commit: expected branch {branch!r}")

    staged = git(folder, "ls-files", "--stage", "-z", strip=False)
    modes = {
        path: meta.split()[0]
        for entry in staged.split("\0")
        if entry
        for meta, path in [entry.split("\t", 1)]
        if meta.split()[2] == "0"
    }
    listed = git(
        folder, "ls-files", "--cached", "--others", "--exclude-standard", "-z", strip=False
    )
    for relative in dict.fromkeys(path for path in listed.split("\0") if path):
        path = folder / relative
        if not path.exists() and not path.is_symlink():
            git(folder, "update-index", "--remove", "--", relative)
            continue
        if path.is_dir():  # existing submodule entry; its object id is already in the index
            continue
        if path.is_symlink():
            mode = "120000"
            oid = git(folder, "hash-object", "-w", "--stdin", input_text=os.readlink(path))
        else:
            previous = modes.get(relative)
            mode = previous if previous in {"100644", "100755"} else "100644"
            oid = git(folder, "hash-object", "-w", "--no-filters", "--", relative)
        git(folder, "update-index", "--add", "--cacheinfo", mode, oid, relative)

    parent = git(folder, "rev-parse", "HEAD")
    if _ok(folder, "diff", "--cached", "--quiet"):
        return parent
    tree = git(folder, "write-tree")
    sha = git(folder, *_IDENTITY, "commit-tree", tree, "-p", parent, "-m", message)
    git(folder, "update-ref", f"refs/heads/{branch}", sha, parent)
    return sha


def diff_stat(folder: Path, base: str, branch: str) -> str:
    """``git diff --shortstat base...branch`` (e.g. "2 files changed, 10 insertions(+)")."""
    out = git(
        folder,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--shortstat",
        f"{base}...{branch}",
        check=False,
    )
    return out or "no changes"


def refuse_protected(folder: Path, branch: str) -> None:
    """Raise ``ProtectedBranchError`` unless ``branch`` is a ``relay/`` task branch."""
    if (
        not branch.startswith(BRANCH_PREFIX)
        or branch in PROTECTED_BRANCHES
        or branch == default_branch(folder)
    ):
        raise ProtectedBranchError(f"refusing to push {branch!r}: only relay/ task branches")


def push_branch(folder: Path, url: str, branch: str, env: Mapping[str, str] | None = None) -> None:
    """Push ``branch`` to the same name at ``url``: one ref, never forced, never a default."""
    refuse_protected(folder, branch)
    ref = f"refs/heads/{branch}"
    git(folder, "push", "--quiet", url, f"{ref}:{ref}", env=_env(env))
