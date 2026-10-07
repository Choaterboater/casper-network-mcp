"""No personal machine paths slip into the repo's own text.

The release workflow already greps for a home path (see ``test_release_files``); this is the
same guard as a unit test over the files we author. Vendor data under ``specs/`` and the CI
config (which quotes the pattern it greps for) are skipped.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("src", "tests")
SCAN_FILES = ("README.md", "CHANGELOG.md", "pyproject.toml")
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".txt"}
SKIP_DIRS = {"__pycache__"}
SELF = pathlib.Path(__file__).resolve()
#: Built from pieces so this file does not contain the very token it forbids.
FORBIDDEN = ("/" + "Users" + "/", "C:" + "\\" + "Users" + "\\")


def _our_text_files():
    seen: set[pathlib.Path] = set()
    for root in SCAN_ROOTS:
        for path in (ROOT / root).rglob("*"):
            if (
                path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and path.resolve() != SELF
                and not any(part in SKIP_DIRS for part in path.parts)
            ):
                seen.add(path)
    for name in SCAN_FILES:
        path = ROOT / name
        if path.is_file():
            seen.add(path)
    return sorted(seen)


def test_no_absolute_home_paths_in_our_text():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _our_text_files()
        if any(token in path.read_text(encoding="utf-8", errors="ignore") for token in FORBIDDEN)
    ]
    assert offenders == [], f"personal home paths found in: {offenders}"
