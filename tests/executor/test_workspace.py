"""Per-idea project folders: stable per idea, distinct per idea, never in relay or outside root."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from relay.config import Settings
from relay.executor.workspace import project_dir
from tests.conftest import REPO_ROOT


def _settings(root: Path) -> Settings:
    return Settings(executor_projects_root=str(root))


def test_same_idea_keeps_its_folder_across_calls_and_renames(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    idea_id = uuid.uuid4()

    first = project_dir(idea_id, "Garden planner", settings)
    (first / "marker.txt").write_text("work in progress")

    assert project_dir(idea_id, "Garden planner", settings) == first
    renamed = project_dir(idea_id, "Totally different name", settings)
    assert renamed == first
    assert (renamed / "marker.txt").read_text() == "work in progress"
    assert [p.name for p in tmp_path.iterdir()] == [first.name]


def test_same_title_different_ideas_get_different_folders(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = project_dir(uuid.uuid4(), "Todo app", settings)
    b = project_dir(uuid.uuid4(), "Todo app", settings)
    assert a != b
    assert a.is_dir() and b.is_dir()


@pytest.mark.parametrize("root", [REPO_ROOT, REPO_ROOT / "projects", REPO_ROOT / "src" / "x"])
def test_root_inside_relay_repo_is_refused(root: Path) -> None:
    with pytest.raises(ValueError, match="inside the relay repository"):
        project_dir(uuid.uuid4(), "anything", _settings(root))
    assert not (REPO_ROOT / "projects").exists()


@pytest.mark.parametrize(
    "title",
    ["../../etc", "/etc/passwd", "a/../../b", "..", "", "   ", "Café résumé 日本語 🚀", "x" * 500],
)
def test_hostile_title_stays_directly_under_root(tmp_path: Path, title: str) -> None:
    idea_id = uuid.uuid4()
    folder = project_dir(idea_id, title, _settings(tmp_path))

    assert folder.parent == tmp_path.resolve()
    assert folder.is_dir()
    assert folder.name.endswith(f"-{idea_id.hex[:8]}")
    assert all(c.isascii() and (c.isalnum() or c == "-") for c in folder.name)
    assert len(folder.name) <= 60
