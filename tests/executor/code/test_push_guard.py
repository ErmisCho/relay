"""AC2: the executor cannot merge or push to the default branch."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import relay.executor.code as code_pkg
from relay.executor.code import git
from tests.executor.code.conftest import github_project, sh


@pytest.mark.parametrize("branch", ["main", "master", "trunk", "feature/x"])
def test_push_refuses_default_and_non_task_branches(tmp_path: Path, branch: str) -> None:
    """Bug caught: a push of the default (or any non-relay/) branch reaching the remote."""
    folder = tmp_path / "proj"
    bare = github_project(folder, tmp_path)
    sh(folder, "git", "branch", "--quiet", "trunk")
    if branch == "trunk":  # a default branch not named main/master, as origin/HEAD reports it
        sh(folder, "git", "update-ref", "refs/remotes/origin/trunk", "HEAD")
        sh(folder, "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    before = sh(bare, "git", "for-each-ref")
    with pytest.raises(git.ProtectedBranchError):
        git.push_branch(folder, "https://github.com/acme/widget.git", branch)
    assert sh(bare, "git", "for-each-ref") == before


def test_push_sends_only_the_task_branch(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    bare = github_project(folder, tmp_path)
    co = git.checkout(folder, "relay/abcd1234-x")
    assert co == {"branch": "relay/abcd1234-x", "base": "main"}
    git.push_branch(folder, "https://github.com/acme/widget.git", "relay/abcd1234-x")
    refs = sh(bare, "git", "for-each-ref", "--format=%(refname)").splitlines()
    assert sorted(refs) == ["refs/heads/main", "refs/heads/relay/abcd1234-x"]


def test_commit_does_not_execute_repository_filters(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    github_project(folder, tmp_path)
    branch = "relay/abcd1234-safe"
    git.checkout(folder, branch)
    sh(folder, "git", "config", "filter.evil.clean", "false")
    (folder / ".gitattributes").write_text("*.txt filter=evil\n")
    (folder / "result.txt").write_text("safe\n")

    git.commit_all(folder, branch, "Safe commit")

    assert git.git(folder, "show", f"{branch}:result.txt") == "safe"


def test_code_executor_has_no_merge_capability() -> None:
    """Bug caught: a merge call (REST merge endpoint, `git merge`, auto-merge) creeping in."""
    sources = {p.name: p.read_text() for p in Path(code_pkg.__file__).parent.glob("*.py")}
    code = {
        n: "\n".join(ln for ln in s.splitlines() if not ln.lstrip().startswith("#"))
        for n, s in sources.items()
    }
    pattern = re.compile(
        r"/merge\b|\"merge\"|'merge'|auto_merge|enablePullRequestAutoMerge|--force"
    )
    assert {n: pattern.findall(s) for n, s in code.items() if pattern.search(s)} == {}
