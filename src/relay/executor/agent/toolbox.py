"""The executor's toolbox: scripts it wrote in one task, reused in later ones.

Lives at ``<executor_projects_root>/.toolbox``. Before a run it is copied into the task folder
as ``toolbox/`` (where the file tools and the shell can reach it); after the run, new or
changed files there are copied back. Copying instead of mounting keeps both shells' sandboxes
(sandbox-exec write rule, Docker mount) at exactly the task folder.

Ceilings, by choice: sync never deletes (removing a script from one task folder does not
remove it from the toolbox), and two tasks finishing together both write back - the later
one wins per file. Add locking or versioning if concurrent tasks start clobbering scripts.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

DIR_NAME = "toolbox"
#: Skipped on sync-back: a toolbox holds scripts, not the data a task produced.
MAX_FILE_BYTES = 256 * 1024
_SKIP_DIRS = {"__pycache__", ".tmp"}


def toolbox_dir(projects_root: Path) -> Path:
    return projects_root / ".toolbox"


def _files(root: Path) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not _SKIP_DIRS.intersection(p.relative_to(root).parts)
    ]


def copy_in(toolbox: Path, workspace: Path) -> list[str]:
    """Copy the toolbox into ``workspace/toolbox``; returns the relative paths copied."""
    target = workspace / DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in _files(toolbox) if toolbox.is_dir() else []:
        rel = src.relative_to(toolbox)
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target / rel)
        copied.append(rel.as_posix())
    return sorted(copied)


def copy_out(workspace: Path, toolbox: Path) -> list[str]:
    """Save new or changed files from ``workspace/toolbox``; returns the relative paths saved."""
    source = workspace / DIR_NAME
    if not source.is_dir():
        return []
    saved = []
    for src in _files(source):
        rel = src.relative_to(source)
        dst = toolbox / rel
        if src.stat().st_size > MAX_FILE_BYTES:
            continue
        if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        saved.append(rel.as_posix())
    return sorted(saved)
