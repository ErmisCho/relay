"""The executor's toolbox: scripts saved in one task reach the next task's folder."""

from __future__ import annotations

from pathlib import Path

from relay.executor.agent import toolbox


def test_a_script_saved_in_one_task_is_there_in_the_next(tmp_path: Path) -> None:
    # Catches: sync-back missing new/changed files, re-reporting unchanged ones, keeping
    # task output or caches in the toolbox, and a deleted script vanishing from the toolbox.
    box = toolbox.toolbox_dir(tmp_path / "projects")
    first, second = tmp_path / "idea-a", tmp_path / "idea-b"

    assert toolbox.copy_in(box, first) == []  # empty toolbox, folder still created
    scripts = first / "toolbox"
    (scripts / "disk_usage.py").write_text("print('du')\n")
    (scripts / "INDEX.md").write_text("disk_usage.py - disk usage - python toolbox/disk_usage.py\n")
    (scripts / "__pycache__").mkdir()
    (scripts / "__pycache__" / "disk_usage.cpython-313.pyc").write_bytes(b"x")
    (scripts / "dump.csv").write_bytes(b"x" * (toolbox.MAX_FILE_BYTES + 1))
    assert toolbox.copy_out(first, box) == ["INDEX.md", "disk_usage.py"]
    assert toolbox.copy_out(first, box) == []  # unchanged: nothing to save

    assert toolbox.copy_in(box, second) == ["INDEX.md", "disk_usage.py"]
    assert (second / "toolbox" / "disk_usage.py").read_text() == "print('du')\n"

    (second / "toolbox" / "disk_usage.py").write_text("print('du -h')\n")
    (second / "toolbox" / "INDEX.md").unlink()
    assert toolbox.copy_out(second, box) == ["disk_usage.py"]
    assert (box / "disk_usage.py").read_text() == "print('du -h')\n"
    assert (box / "INDEX.md").is_file()  # sync never deletes
