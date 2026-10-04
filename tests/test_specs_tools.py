from casper_network_mcp import sdk_compat, specs_index
from casper_network_mcp.products import specs_tools


def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(specs_index, "cache_dir", lambda: tmp_path / "cache")
    specs_index.clear_lookup_cache()


def test_backend_has_the_two_read_tools():
    server = specs_tools.backend()
    assert sdk_compat.tool_names(server) == ["list_api_families", "lookup_api"]
    for name in ("list_api_families", "lookup_api"):
        tool = sdk_compat.get_tool(server, name)
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False


def test_lookup_api_answers_from_the_bundle(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    hits = specs_tools.lookup_api("GET /api/v1/self", product="mist")
    assert hits and hits[0]["path"] == "/api/v1/self"


def test_lookup_api_refuses_unknown_product(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    out = specs_tools.lookup_api("wlan", product="aos8")
    assert out[0]["error"].startswith("Unknown product")


def test_lookup_api_reports_a_broken_index_plainly(monkeypatch):
    def broken(*a, **k):
        raise FileNotFoundError("index gone")

    monkeypatch.setattr(specs_index, "lookup", broken)
    assert specs_tools.lookup_api("wlan") == [{"error": "index gone", "degraded": True}]


def test_list_api_families():
    out = specs_tools.list_api_families("central", limit=5)
    assert out["product"] == "central" and len(out["families"]) == 5 and out["total"] >= 5
    assert {"title", "operations", "sample"} <= set(out["families"][0])
    assert specs_tools.list_api_families("mist")["total"] >= 1
    assert "error" in specs_tools.list_api_families("glp")
