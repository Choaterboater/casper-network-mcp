"""Generated tools: built only from the bundled specs, and every call goes through the product client."""

from __future__ import annotations

import inspect
import json
from importlib.resources import files

import pytest

from casper_network_mcp import sdk_compat
from casper_network_mcp.openapi_gen.manifest import build_product_manifest, dumps, load_manifest
from casper_network_mcp.openapi_gen.runtime import generated_backend
from casper_network_mcp.specs_bundle import operations

PRODUCTS = ("central", "mist", "clearpass")


class FakeClient:
    """Anything with the product client's ``request()`` signature."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, method, path, *, params=None, json=None, kind=None, **extra):
        self.calls.append({"method": method, "path": path, "params": params, "json": json, "kind": kind, **extra})
        return {"ok": True}


def generated_tools(product: str):
    backend = generated_backend(product, client=FakeClient)
    return list(sdk_compat.tool_registry(backend).values())


def test_every_generated_operation_is_in_a_bundled_spec():
    for product in PRODUCTS:
        known = {(o.method, o.path) for o in operations(product)}
        missing = [(t.method, t.path) for t in load_manifest(product).tools if (t.method, t.path) not in known]
        assert not missing, (product, missing[:5])


def test_every_bundled_operation_has_a_generated_tool():
    for product in PRODUCTS:
        built = {(t.method, t.path) for t in load_manifest(product).tools}
        assert built == {(o.method, o.path) for o in operations(product)}, product


def test_generated_tools_have_no_confirm_and_preview_is_opt_in():
    for product in PRODUCTS:
        for tool in generated_tools(product):
            params = inspect.signature(tool.fn).parameters
            assert "confirm" not in params, tool.name
            if "dry_run" in params:
                assert params["dry_run"].default is False, tool.name


@pytest.mark.parametrize("product", PRODUCTS)
def test_committed_manifest_matches_a_fresh_build(product):
    committed = (files("casper_network_mcp") / "openapi_gen" / "manifests" / f"{product}.json").read_text("utf-8")
    assert committed == dumps(build_product_manifest(product)), "run scripts/build_manifests.py"


def test_manifests_name_only_bundled_files():
    bundled = {
        d["path"]
        for d in json.loads((files("casper_network_mcp") / "specs" / "MANIFEST.json").read_text())["documents"]
    }
    for product in PRODUCTS:
        for source in load_manifest(product).source["files"]:
            assert source["file"] in bundled


def test_clearpass_paths_go_under_the_api_base():
    assert load_manifest("clearpass").base_path == "/api"
    assert load_manifest("mist").base_path == ""
    assert load_manifest("central").base_path == ""


async def test_generated_call_goes_through_the_client_request():
    client = FakeClient()
    backend = generated_backend("mist", client=lambda: client)
    tool = next(t for t in sdk_compat.tool_registry(backend).values() if t.name == "mist_list_site_wlans")
    out = await tool.fn(site_id="s1")
    assert out == {"ok": True}
    assert client.calls[0]["method"] == "GET"
    assert client.calls[0]["path"] == "/api/v1/sites/s1/wlans"


async def test_clearpass_generated_call_has_the_api_prefix():
    client = FakeClient()
    backend = generated_backend("clearpass", client=lambda: client)
    tool = next(t for t in sdk_compat.tool_registry(backend).values() if t.name == "clearpass_token_info_me_get")
    await tool.fn()
    assert client.calls[0]["path"] == "/api/oauth/me"


async def test_dry_run_returns_what_would_be_sent_and_sends_nothing():
    client = FakeClient()
    backend = generated_backend("mist", client=lambda: client)
    tool = next(t for t in sdk_compat.tool_registry(backend).values() if t.name == "mist_update_site_wlan")
    out = await tool.fn(site_id="s1", wlan_id="w1", body={"vlan_id": 30}, dry_run=True)
    assert out["would_send"]["method"] == "PUT"
    assert out["would_send"]["path"] == "/api/v1/sites/s1/wlans/w1"
    assert out["would_send"]["body"] == {"vlan_id": 30}
    assert client.calls == []


async def test_path_piece_that_moves_the_request_is_refused_before_sending():
    client = FakeClient()
    backend = generated_backend("mist", client=lambda: client)
    tool = next(t for t in sdk_compat.tool_registry(backend).values() if t.name == "mist_update_site_wlan")
    for bad in ("abc/../../orgs/X", "a?b=1", "a#b", "..", "a%2fb"):
        out = await tool.fn(site_id=bad, wlan_id="w1", body={"vlan_id": 30})
        assert "error" in out, bad
    assert client.calls == []


async def test_client_errors_become_plain_error_results():
    from casper_network_mcp.core.errors import ToolError

    class Nope(ToolError):
        def as_error(self):
            return {"error": "nope"}

    class Refusing(FakeClient):
        async def request(self, *a, **k):
            raise Nope("nope")

    backend = generated_backend("mist", client=Refusing)
    tool = next(t for t in sdk_compat.tool_registry(backend).values() if t.name == "mist_update_site_wlan")
    assert await tool.fn(site_id="s1", wlan_id="w1", body={}) == {"error": "nope"}


def test_manifest_names_are_unique_and_counts_agree():
    for product in PRODUCTS:
        m = load_manifest(product)
        names = [t.name for t in m.tools]
        assert len(names) == len(set(names)) == m.source["operation_count"], product
        assert all(name.startswith(f"{product}_") and len(name) <= 60 for name in names)


def test_manifest_files_are_utf8_json():
    for product in PRODUCTS:
        raw = (files("casper_network_mcp") / "openapi_gen" / "manifests" / f"{product}.json").read_bytes()
        json.loads(raw.decode("utf-8"))


def test_central_api_families_are_all_generated():
    prefixes = {t.path.split("/")[1] for t in load_manifest("central").tools}
    assert {"network-config", "network-monitoring", "network-troubleshooting", "network-services"} <= prefixes


def test_no_generated_tool_takes_a_login_argument():
    for product in PRODUCTS:
        for tool in generated_tools(product):
            props = set(tool.parameters.get("properties") or {})
            assert not props & {"authorization", "cookie", "x_auth_token", "x_api_key", "x_csrftoken"}, tool.name
