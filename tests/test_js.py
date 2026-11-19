"""Run the browser tests from pytest, so `pytest` (and CI) actually executes them.

`tests/js/*.test.mjs` are plain node scripts. Until now nothing ran them: pytest
collects `tests/`, `pyproject.toml` sets ``testpaths = ["tests"]``, and the CI
workflow only calls pytest — so a JS-only regression shipped green. This wrapper
is the bridge. It **skips** rather than fails on a machine without node, and on
failure it replays node's own output so the failing assertion is readable in the
pytest report.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_the_browser_suite_passes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    files = sorted(glob.glob(str(ROOT / "tests" / "js" / "*.test.mjs")))
    assert files, "no browser tests were found"

    # Node 26 refuses a bare directory, so every file is named explicitly. The
    # working directory is the repo root: the modules resolve their assets
    # relative to the file, and any future fixture will want repo-relative paths.
    result = subprocess.run(
        [node, "--test", *files],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert result.returncode == 0, (
        "node --test failed\n\n" + (result.stdout or "") + (result.stderr or "")
    )
