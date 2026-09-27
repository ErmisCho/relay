"""The code runner finds and runs the tests of a stdlib-only Python project (TASK-53).

Bug caught: a script plus ``tests/`` without ``pyproject.toml`` was reported as "no tests were
found to run", and a found project ran ``python3 -m pytest`` on a python3 without pytest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from relay.executor.code.runner import detect_test_command

SCRIPT = "def four() -> int:\n    return 4\n"
UNITTEST = (
    "import unittest\n\nimport tool\n\n\n"
    "class T(unittest.TestCase):\n    def test_four(self) -> None:\n"
    "        self.assertEqual(tool.four(), {want})\n"
)


@pytest.mark.skipif(
    not Path("/bin/sh").exists(), reason="the command targets the POSIX sandbox shell"
)
@pytest.mark.parametrize("layout", ["tests/test_tool.py", "test_tool.py"])
@pytest.mark.parametrize(("want", "exit_ok"), [(4, True), (5, False)])
def test_stdlib_project_tests_are_found_and_run(
    tmp_path: Path, layout: str, want: int, exit_ok: bool
) -> None:
    (tmp_path / "tool.py").write_text(SCRIPT)
    test = tmp_path / layout
    test.parent.mkdir(exist_ok=True)
    test.write_text(UNITTEST.format(want=want))

    command = detect_test_command(tmp_path)
    assert command is not None
    # The sandbox's python3 is the system one, without pytest: drop this venv from PATH.
    path = [p for p in os.environ["PATH"].split(os.pathsep) if not p.startswith(sys.prefix)]
    env = {"PATH": os.pathsep.join(path)}
    proc = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, env=env, capture_output=True)
    assert b"import pytest" not in proc.stderr
    assert (proc.returncode == 0) is exit_ok, proc.stdout + proc.stderr


def test_folder_without_tests_has_no_test_command(tmp_path: Path) -> None:
    (tmp_path / "tool.py").write_text(SCRIPT)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'tool'\n")
    assert detect_test_command(tmp_path) is None
