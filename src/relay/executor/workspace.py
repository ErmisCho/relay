"""Per-idea project folders for the executor, outside the relay repository.

Every idea the user asks to build gets its own folder under ``settings.executor_projects_root``;
the executor's coding/filesystem tools run rooted there, so work on an idea never touches relay's
own source tree.
"""

from __future__ import annotations

import pathlib
import re
import unicodedata
import uuid

import relay
from relay.config import Settings

_SLUG_MAX_LEN = 40
_ID_LEN = 8


def _slugify(title: str) -> str:
    """Lowercase ASCII slug of ``title`` using only ``[a-z0-9-]``; ``idea`` when nothing is left."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    slug = slug[:_SLUG_MAX_LEN].rstrip("-")
    return slug or "idea"


def repo_root() -> pathlib.Path:
    """Directory holding relay's ``pyproject.toml``; the package dir itself if none is found."""
    package_dir = pathlib.Path(relay.__file__).resolve().parent
    for candidate in (package_dir, *package_dir.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return package_dir


def _checked_root(settings: Settings) -> pathlib.Path:
    root = pathlib.Path(settings.executor_projects_root).expanduser().resolve()
    if root == pathlib.Path(root.anchor):
        raise ValueError(f"executor_projects_root must not be the filesystem root: {root}")
    if root == pathlib.Path.home().resolve():
        raise ValueError(f"executor_projects_root must not be the home directory itself: {root}")
    repo = repo_root()
    if root == repo or root.is_relative_to(repo):
        raise ValueError(
            f"executor_projects_root {root} is inside the relay repository {repo}; "
            "idea projects must live outside it"
        )
    return root


def project_dir(idea_id: uuid.UUID, title: str, settings: Settings) -> pathlib.Path:
    """Return (creating it if needed) the absolute project folder for one idea.

    The folder is ``<root>/<slug>-<first 8 hex chars of idea_id>``, where ``<root>`` is
    ``settings.executor_projects_root`` with ``~`` expanded and resolved, and ``<slug>`` is a
    bounded, lowercase ASCII slug of ``title`` (``idea`` when the title has no usable chars).

    The folder is keyed by ``idea_id``, not by title: if a folder ending in ``-<id8>`` already
    exists under the root (e.g. created before the idea was renamed), that folder is reused and
    the current ``title`` is ignored. So the same idea always maps to the same folder, and two
    ideas with the same title map to different ones.

    Raises ``ValueError`` when the root is ``/``, the user's home directory itself, or inside
    the relay repository.
    """
    root = _checked_root(settings)
    suffix = f"-{idea_id.hex[:_ID_LEN]}"
    if root.is_dir():
        # Linear scan of the root — fine for hundreds of ideas; index by id if that grows large.
        # Symlinks are skipped: a planted `x-<id8>` link would re-root the file tools and the
        # shell sandbox (whose write rule is the workspace) onto its target.
        existing = sorted(
            p
            for p in root.iterdir()
            if p.name.endswith(suffix) and not p.is_symlink() and p.is_dir()
        )
        if existing:
            return existing[0].resolve()
    target = root / f"{_slugify(title)}{suffix}"
    if target.is_symlink():
        raise ValueError(f"project folder {target} is a symlink; refusing to use it")
    target.mkdir(parents=True, exist_ok=True)
    return target


def projects_root(settings: Settings) -> pathlib.Path:
    """``settings.executor_projects_root``, resolved and checked like :func:`project_dir`."""
    return _checked_root(settings)
