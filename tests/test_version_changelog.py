"""The release version and its changelog entry stay in step."""

from __future__ import annotations

import pathlib
import tomllib

from casper_network_mcp import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_version_is_0_1_3():
    assert __version__ == "0.1.3"


def test_pyproject_and_package_agree():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_changelog_has_the_release():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {__version__}" in text
