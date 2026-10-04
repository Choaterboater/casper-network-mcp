"""Release files: notices in step, plain README promises, and CI/release workflows that do what the plan says."""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASPER_INSTALL = "uv pip install --require-hashes --no-deps --only-binary :all:"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    # YAML reads the key "on" as True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def _steps_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", "")) for step in job["steps"])


def test_top_notice_carries_the_specs_notice_word_for_word():
    top = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    specs = (ROOT / "src" / "casper_network_mcp" / "specs" / "NOTICE.md").read_text(encoding="utf-8")
    assert top.endswith(specs)


def test_readme_says_what_it_reads_and_what_it_ships():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "uvx --from casper-network-mcp==0.1.0 casper-network-mcp --read-only" in readme
    for name in ("MIST_API_TOKEN", "MIST_HOST", "CENTRAL_BASE_URL", "CENTRAL_CLIENT_ID", "CENTRAL_CLIENT_SECRET",
                 "CLEARPASS_BASE_URL", "CLEARPASS_API_TOKEN"):  # fmt: skip
        assert name in readme, name
    assert "There are no write switches" in readme
    assert "HPE's proprietary API documents (not MIT; see specs/NOTICE.md)" in readme
    assert "Juniper Mist's MIT" in readme


def test_readme_says_clearpass_tokens_expire_and_what_happens():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ClearPass API tokens expire" in readme
    assert '"login": "expired"' in readme
    assert '{"error": "login_expired", "product": ...}' in readme


def test_security_policy_uses_private_reports_only():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "private vulnerability reporting" in text
    assert "@" not in text


def test_ci_runs_lint_types_tests_build_and_scrub():
    ci = _workflow("ci.yml")
    assert {"push", "pull_request"} <= set(ci["on"])
    job = ci["jobs"]["test"]
    matrix = job["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest"}
    assert set(matrix["python"]) == {"3.11", "3.13"}
    text = _steps_text(job)
    for needed in ("uv sync", "ruff check", "mypy src/casper_network_mcp/core", "pytest -q", "uv build",
                   "build_index.py --check", "build_manifests.py --check"):  # fmt: skip
        assert needed in text, needed
    scrub = _steps_text(ci["jobs"]["scrub"])
    for needed in ("RFC 1918", "home path", "env file loading", "RAG or", "browser identity"):
        assert needed in scrub, needed
    assert ci.get("permissions") == {"contents": "read"}


def test_release_builds_locks_checks_then_publishes_the_same_files():
    rel = _workflow("release.yml")
    assert rel["on"] == {"push": {"tags": ["v*"]}}
    build = rel["jobs"]["build"]
    text = _steps_text(build)
    assert "uv build" in text and "scripts/make_lock.py" in text
    check = rel["jobs"]["check-lock"]
    assert set(check["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest"}
    assert set(check["strategy"]["matrix"]["python"]) == {"3.11", "3.13"}
    check_text = _steps_text(check)
    assert CASPER_INSTALL in check_text and "--find-links dist" in check_text
    assert '/venv/bin/casper-network-mcp" --help' in check_text
    publish = rel["jobs"]["publish"]
    assert publish["needs"] == ["build", "check-lock"]
    assert publish["permissions"] == {"id-token": "write"}
    assert "pypa/gh-action-pypi-publish" in _steps_text(publish)
    attach = rel["jobs"]["attach-lock"]
    assert "publish" in attach["needs"]
    assert "casper-network-mcp.lock.txt" in _steps_text(attach)
    assert rel.get("permissions") == {"contents": "read"}


def test_example_config_starts_read_only_with_no_logins_in_it():
    import json

    servers = json.loads((ROOT / ".mcp.json.example").read_text(encoding="utf-8"))["mcpServers"]
    assert len(servers) == 1
    (entry,) = servers.values()
    assert entry["type"] == "stdio"
    assert entry["args"][-1] == "--read-only"
    assert "casper-network-mcp" in entry["args"]
    # Logins come from the shell, never from a file that gets copied around.
    assert "env" not in entry


def test_third_party_notice_credits_the_reference_project_honestly():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    # No placeholder in place of a copyright line, and no claim that copied code is nowireless4u's.
    assert "exact copyright line" not in text
    assert "names this project as its MIT source" not in text
    assert "nowireless4u/hpe-networking-mcp" in text and "reference" in text
    headers = [
        p.relative_to(ROOT)
        for p in [*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
        if re.search(r"MIT,\s+" + "nowireless4u", p.read_text(encoding="utf-8"))
    ]
    assert not headers, headers


def test_top_notice_says_what_the_built_files_hold_and_how_to_remove_it():
    top = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    summary = top.split("The rest of this file is")[0]
    assert "one-line" not in summary
    assert "descriptions" in summary
    assert "scripts/build_manifests.py" in summary and "scripts/build_index.py" in summary
