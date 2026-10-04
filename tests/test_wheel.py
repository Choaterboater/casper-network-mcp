"""The built wheel: runs with no logins, carries its data and notices, and installs from the hash-locked file.

These tests build the wheel with ``uv build`` and install it into a fresh
virtual environment with Casper's exact install command. They use uv's local
cache (``--offline``), so they never download anything; CI runs them after
``uv sync`` has filled the cache.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

from casper_network_mcp import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASPER_INSTALL = ["--require-hashes", "--no-deps", "--only-binary", ":all:"]


def _uv() -> str:
    found = shutil.which("uv") or os.environ.get("UV")  # `uv run` sets UV to its own path
    assert found and pathlib.Path(found).exists(), "uv is needed to build and install the wheel"
    return found


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> pathlib.Path:
    dist = tmp_path_factory.mktemp("dist")
    out = _run([_uv(), "build", "--offline", "--wheel", "-o", str(dist)], cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return dist


def _wheel(dist: pathlib.Path) -> pathlib.Path:
    wheels = list(dist.glob("casper_network_mcp-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _empty_env(home: pathlib.Path) -> dict[str, str]:
    """No login variables, nothing from this machine but a bare PATH."""
    return {"PATH": os.defpath, "HOME": str(home)}


def _help_runs(venv: pathlib.Path, home: pathlib.Path) -> None:
    out = _run([str(venv / "bin" / "casper-network-mcp"), "--help"], cwd=home, env=_empty_env(home))
    assert out.returncode == 0, out.stderr
    assert "--read-only" in out.stdout


def _venv(path: pathlib.Path) -> pathlib.Path:
    out = _run([_uv(), "venv", "--offline", "-q", "--python", sys.executable, str(path)])
    assert out.returncode == 0, out.stderr
    return path


def test_wheel_contents(built):
    with zipfile.ZipFile(_wheel(built)) as whl:
        names = whl.namelist()
        info = f"casper_network_mcp-{__version__}.dist-info"
        metadata = whl.read(f"{info}/METADATA").decode()
        notice = whl.read(f"{info}/licenses/NOTICE.md").decode()
        third_party = whl.read(f"{info}/licenses/THIRD_PARTY_NOTICES.md").decode()
    for path in (
        "casper_network_mcp/specs/MANIFEST.json",
        "casper_network_mcp/specs/NOTICE.md",
        "casper_network_mcp/openapi_gen/manifests/mist.json",
        "casper_network_mcp/openapi_gen/manifests/central.json",
        "casper_network_mcp/openapi_gen/manifests/clearpass.json",
        "casper_network_mcp/router/index.json",
        "casper_network_mcp/router/synonyms.yaml",
        "casper_network_mcp/products/mist/labels.yaml",
        f"{info}/licenses/LICENSE",
        f"{info}/licenses/NOTICE.md",
        f"{info}/licenses/THIRD_PARTY_NOTICES.md",
        f"{info}/licenses/src/casper_network_mcp/specs/NOTICE.md",
    ):
        assert path in names, path
    assert "License-Expression: MIT AND LicenseRef-HPE-API-Documentation" in metadata
    for bad in ("ingestion", "data/", ".env", "tests/", "__pycache__", ".pyc"):
        assert not [n for n in names if bad in n], bad
    assert "Placeholder" not in notice and "Placeholder" not in third_party
    assert "nowireless4u" in third_party and "mistsys" in third_party


def test_wheel_runs_with_no_logins(built, tmp_path):
    pins = tmp_path / "pins.txt"
    export = [_uv(), "export", "--offline", "--frozen", "--format", "requirements-txt", "--no-dev"]
    out = _run([*export, "--hashes", "--no-emit-project", "-o", str(pins)], cwd=ROOT)
    assert out.returncode == 0, out.stderr
    venv = _venv(tmp_path / "venv")
    install = [_uv(), "pip", "install", "--offline", "--python", str(venv / "bin" / "python"), "--no-deps"]
    out = _run([*install, "--only-binary", ":all:", "-r", str(pins)])
    assert out.returncode == 0, out.stderr
    out = _run([*install, str(_wheel(built))])
    assert out.returncode == 0, out.stderr
    _help_runs(venv, tmp_path)


def test_release_lock_installs_with_caspers_command(built, tmp_path):
    lock = tmp_path / "casper-network-mcp.lock.txt"
    out = _run(
        [sys.executable, "scripts/make_lock.py", __version__, "--dist", str(built), "--out", str(lock), "--offline"],
        cwd=ROOT,
    )
    assert out.returncode == 0, out.stderr
    lines = lock.read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if line.startswith(("-e", "."))]
    digest = hashlib.sha256(_wheel(built).read_bytes()).hexdigest()
    own = [line for line in lines if line.startswith("casper-network-mcp==")]
    assert own == [f"casper-network-mcp=={__version__} --hash=sha256:{digest}"]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)

    venv = _venv(tmp_path / "venv")
    install = [_uv(), "pip", "install", "--offline", "--python", str(venv / "bin" / "python")]
    out = _run([*install, *CASPER_INSTALL, "--find-links", str(built), "-r", str(lock)])
    assert out.returncode == 0, out.stderr
    _help_runs(venv, tmp_path)


def test_make_lock_refuses_a_wrong_version(built, tmp_path):
    out = _run(
        [sys.executable, "scripts/make_lock.py", "9.9.9", "--dist", str(built), "--out", str(tmp_path / "x.txt")],
        cwd=ROOT,
    )
    assert out.returncode != 0
    assert not (tmp_path / "x.txt").exists()
