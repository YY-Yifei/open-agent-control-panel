"""The version the package reports is the version it is published as.

A release that bumped `pyproject.toml` but not `oacp.__version__` shipped twice
(0.4.1 and 0.4.2) while `--version`, `/health` and the `User-Agent` still said
0.4.0. One test is cheaper than remembering.
"""

from __future__ import annotations

import re
from pathlib import Path

import oacp

ROOT = Path(__file__).resolve().parents[1]

_DECLARED = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)


def _declared_version() -> str:
    """Read `project.version` out of pyproject.toml without needing tomllib (3.11+)."""
    match = _DECLARED.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert match is not None, "pyproject.toml declares no project.version"
    return match.group(1)


def test_the_package_reports_the_version_it_is_published_as() -> None:
    assert oacp.__version__ == _declared_version()


def test_the_changelog_documents_the_version_being_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{oacp.__version__}]" in changelog, (
        "a release needs its own changelog section; rename [Unreleased] before tagging"
    )


def test_the_version_is_a_plain_release_number() -> None:
    # twine and PyPI both refuse what pip would accept, so the shape is checked here.
    assert re.fullmatch(r"\d+\.\d+\.\d+", oacp.__version__), oacp.__version__
