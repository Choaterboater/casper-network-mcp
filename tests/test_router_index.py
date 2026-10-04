"""find_tool's word index: shipped in the package, current, and rebuilt in memory when missing."""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files

import yaml

from casper_network_mcp.router import find, index


def test_shipped_index_matches_the_tools():
    shipped = (files("casper_network_mcp.router") / "index.json").read_text(encoding="utf-8")
    assert shipped == index.dumps_index(index.build_index()), "run scripts/build_index.py"


def test_build_index_check_mode_passes():
    out = subprocess.run(
        [sys.executable, "scripts/build_index.py", "--check"], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr


def test_find_tool_answers_without_loading_any_backend():
    code = (
        "from casper_network_mcp.router import find, index\n"
        "hits = find.find_tool('bounce a switch port', top_k=3, product='central')\n"
        "assert hits and hits[0]['kind'] == 'disruptive', hits\n"
        "assert index._cached.cache_info().currsize == 0, 'catalog was built'\n"
        "import sys; assert 'casper_network_mcp.products.central.monitoring' not in sys.modules\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


def test_missing_index_is_rebuilt_in_memory(monkeypatch):
    expected = index._from_data(index.build_index())
    monkeypatch.setattr(index, "_read_shipped", lambda: None)
    index._cached_index.cache_clear()
    try:
        rebuilt = index.load_index()
        assert [e.name for e in rebuilt.entries] == [e.name for e in expected.entries]
    finally:
        index._cached_index.cache_clear()


def test_index_of_another_version_is_not_used(monkeypatch, tmp_path):
    monkeypatch.setattr(index, "files", lambda _pkg: tmp_path)
    (tmp_path / "index.json").write_text('{"version": 0, "tools": [], "name_postings": {}}', encoding="utf-8")
    assert index._read_shipped() is None
    (tmp_path / "index.json").write_text("not json", encoding="utf-8")
    assert index._read_shipped() is None


def test_include_schema_still_gives_the_tool_schema():
    hit = find.find_tool("bounce port 7 on the closet switch", top_k=1, product="central", include_schema=True)[0]
    assert hit["name"] == "port_bounce"
    assert "properties" in hit["schema"]


def test_synonyms_file_is_plain_lower_case_words():
    raw = yaml.safe_load((files("casper_network_mcp.router") / "synonyms.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    for key, words in raw.items():
        assert isinstance(key, str) and key == key.lower(), key
        assert isinstance(words, list) and words, key
        assert all(isinstance(w, str) and w == w.lower() and " " not in w for w in words), key


def test_a_spread_synonym_is_one_idea():
    groups = find.query_groups("who is on the guest wifi")
    wifi = next(g for g in groups if "wifi" in g)
    assert {"wlan", "ssid", "wireless"} <= wifi
    assert any("client" in g for g in groups)


def test_question_intent():
    assert find.intent("what alerts are open") == "read"
    assert find.intent("please create VLAN 30") == "create"
    assert find.intent("remove an endpoint") == "delete"
    assert find.intent("change the vlan on the ssid") == "update"
    assert find.intent("bounce port 7") is None


def test_plurals_fold_to_one_word():
    assert index.fold("sites") == "site"
    assert index.fold("policies") == "policy"
    assert index.fold("switches") == "switch"
    assert index.fold("access") == "access"
    assert index.fold("ap") == "ap"
