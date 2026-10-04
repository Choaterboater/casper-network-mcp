"""The generator: parsing, naming, manifests and tool registration.

Ported from hpe-networking-mcp ``tests/unit/test_openapi_gen.py`` (MIT,
nowireless4u/hpe-networking-mcp), rewritten for tools that send through the
product client's ``request()`` and have no ``confirm`` step.
"""

from __future__ import annotations

import copy

import pytest
from mcp.server.mcpserver import MCPServer

from casper_network_mcp import sdk_compat
from casper_network_mcp.openapi_gen.ir import OpenApiError, SpecParser, UnresolvedRefError
from casper_network_mcp.openapi_gen.manifest import Manifest, build_merged_manifest, dumps, sha256_bytes
from casper_network_mcp.openapi_gen.naming import DuplicateNameError, NameAllocator, base_name, snake
from casper_network_mcp.openapi_gen.runtime import _py_type, is_transport_header, register_generated_tools

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Demo", "version": "1.0", "license": {"name": "MIT"}},
    "components": {
        "parameters": {
            "org_id": {"name": "org_id", "in": "path", "required": True, "schema": {"type": "string"}},
            "verbose": {"name": "verbose", "in": "query", "schema": {"type": "boolean", "default": False}},
        },
        "schemas": {
            "base": {"type": "object", "properties": {"a": {"type": "string"}}},
            "widget": {
                "allOf": [
                    {"$ref": "#/components/schemas/base"},
                    {"type": "object", "properties": {"b": {"type": "integer"}}},
                ]
            },
            "claim_codes": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["fast", "slow"]},
        },
    },
    "paths": {
        "/api/v1/orgs/{org_id}/widgets": {
            "get": {
                "operationId": "listWidgets",
                "summary": "List widgets",
                "parameters": [
                    {"$ref": "#/components/parameters/org_id"},
                    {"$ref": "#/components/parameters/verbose"},
                    {"name": "mode", "in": "query", "schema": {"$ref": "#/components/schemas/mode"}},
                    {"name": "site_ids", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
                    {"name": "Content-Type", "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "X-Forwarded-For", "in": "header", "schema": {"type": "string"}},
                    {"name": "X-Envoy-External-Address", "in": "header", "schema": {"type": "string"}},
                    {"name": "Host", "in": "header", "schema": {"type": "string"}},
                    {"name": "If-Match", "in": "header", "schema": {"type": "string"}},
                    {"name": "Tenant-Acid", "in": "header", "schema": {"type": "string"}},
                    {"name": "Idempotency-Key", "in": "header", "schema": {"type": "string"}},
                ],
            },
            "post": {
                "operationId": "createWidget",
                "summary": "Create widget",
                "parameters": [
                    {"$ref": "#/components/parameters/org_id"},
                    {"name": "mode", "in": "query", "schema": {"$ref": "#/components/schemas/mode"}},
                ],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/widget"}}},
                },
            },
            "delete": {
                "operationId": "deleteWidgets",
                "summary": "Delete widgets",
                "parameters": [{"$ref": "#/components/parameters/org_id"}],
            },
        },
    },
}


def _manifest_dict():
    return build_merged_manifest([("demo.json", "deadbeef", SPEC)], platform="demo")


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    async def request(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        return {"ok": True}


def _register(manifest=None, kind_of=None):
    data = manifest or _manifest_dict()
    m = Manifest(
        product="demo", base_path=data.get("base_path", ""), source=data["source"], operations=data["operations"]
    )
    server = MCPServer("demo-core")
    rec = Recorder()
    kwargs = {"kind_of": kind_of} if kind_of else {}
    names = register_generated_tools(server, m, client=lambda: rec, **kwargs)
    return server, names, rec


def _tool(server, name):
    return sdk_compat.get_tool(server, name)


# ── Parsing ─────────────────────────────────────────────────────────────────


def test_parser_resolves_refs_params_and_bodies():
    ops = SpecParser(SPEC).operations()
    assert [o.method for o in ops] == ["GET", "POST", "DELETE"]
    params = {p.name: p for p in ops[0].parameters}
    assert params["org_id"].location == "path" and params["org_id"].required
    assert params["verbose"].schema_type == "boolean" and params["verbose"].default is False
    assert params["mode"].enum == ["fast", "slow"]
    assert params["site_ids"].schema_type == "array" and params["site_ids"].item_type == "string"
    post = ops[1]
    assert post.request_body.schema_type == "object"
    assert post.request_body.content_type == "application/json"
    assert post.request_body.required is True


def test_parser_array_body_item_type():
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/x": {
                "post": {
                    "operationId": "claim",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/claim_codes"}}}
                    },
                }
            }
        },
        "components": SPEC["components"],
    }
    op = SpecParser(spec).operations()[0]
    assert op.request_body.schema_type == "array" and op.request_body.item_type == "string"


def test_parser_raises_on_unresolved_ref():
    spec = {
        "openapi": "3.1.0",
        "paths": {"/api/v1/x": {"get": {"operationId": "g", "parameters": [{"$ref": "#/components/parameters/x"}]}}},
    }
    with pytest.raises(UnresolvedRefError):
        SpecParser(spec).operations()


def test_parser_rejects_unsupported_version():
    with pytest.raises(OpenApiError):
        SpecParser({"swagger": "1.2", "paths": {}})


# ── Naming ──────────────────────────────────────────────────────────────────


def test_snake_and_base_name():
    assert snake("listOrgSites") == "list_org_sites"
    assert base_name("demo", "GET", "/x", "listOrgSites") == "demo_list_org_sites"


def test_name_allocator_fails_on_unresolved_duplicate():
    alloc = NameAllocator()
    alloc.allocate("demo", "GET", "/api/v1/x", "dup")
    with pytest.raises(DuplicateNameError):
        alloc.allocate("demo", "GET", "/api/v1/x", "dup")


def test_name_allocator_disambiguates_distinct_paths():
    alloc = NameAllocator()
    assert alloc.allocate("demo", "GET", "/api/v1/a", "same") != alloc.allocate("demo", "GET", "/api/v1/b", "same")


# ── Manifests ───────────────────────────────────────────────────────────────


def test_manifest_is_deterministic_and_records_source():
    m1, m2 = _manifest_dict(), _manifest_dict()
    assert dumps(m1) == dumps(m2)
    assert m1["source"]["operation_count"] == 3
    assert m1["source"]["files"][0]["sha256"] == "deadbeef"
    assert all("capability" not in op for op in m1["operations"])


def test_sha256_bytes_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")


def test_merged_manifest_is_deterministic_and_deduplicates_operations():
    second = {
        "openapi": "3.0.3",
        "info": {"title": "Second", "version": "2"},
        "paths": {
            "/api/v1/orgs/{org_id}/widgets": {
                "get": {
                    "operationId": "duplicateListWidgets",
                    "parameters": [
                        {"$ref": "#/components/parameters/org_id"},
                        {"name": "scope-id", "in": "query", "required": True, "schema": {"type": "string"}},
                    ],
                }
            },
            "/api/v1/health": {"get": {"operationId": "getHealth"}},
        },
        "components": {"parameters": SPEC["components"]["parameters"]},
    }
    docs = [("b.json", "bbb", second), ("a.json", "aaa", SPEC)]
    merged = build_merged_manifest(docs, platform="demo")
    assert merged["source"]["operation_count"] == 4
    assert merged["source"]["duplicate_operation_count"] == 1
    assert merged["duplicate_operations"][0]["kept_source"] == "a.json"
    duplicate = next(op for op in merged["operations"] if op["key"] == "GET /api/v1/orgs/{org_id}/widgets")
    assert any(p["name"] == "scope-id" and p["required"] for p in duplicate["parameters"])
    assert dumps(merged) == dumps(build_merged_manifest(list(reversed(docs)), platform="demo"))


def test_head_and_options_operations_are_not_tools():
    spec = copy.deepcopy(SPEC)
    spec["paths"]["/api/v1/orgs/{org_id}/widgets"]["head"] = {"operationId": "headWidgets"}
    merged = build_merged_manifest([("demo.json", "x", spec)], platform="demo")
    assert {op["method"] for op in merged["operations"]} == {"GET", "POST", "DELETE"}


# ── Registration ────────────────────────────────────────────────────────────


def test_registration_exposes_typed_params_without_auth():
    server, names, _ = _register()
    assert len(names) == 3
    get_tool = _tool(server, "demo_list_widgets")
    props = get_tool.parameters.get("properties") or {}
    assert {"org_id", "verbose", "mode", "site_ids"} <= set(props)
    site_ids_schema = next(v for v in props["site_ids"].get("anyOf", [props["site_ids"]]) if v.get("type") == "array")
    assert site_ids_schema["items"] == {"type": "string"}
    mode_schema = next(v for v in props["mode"].get("anyOf", [props["mode"]]) if "enum" in v)
    assert mode_schema == {"enum": ["fast", "slow"], "type": "string"}
    assert {"x_trace", "if_match", "tenant_acid", "idempotency_key"} <= set(props)
    assert {"authorization", "content_type", "x_forwarded_for", "x_envoy_external_address", "host"}.isdisjoint(props)
    assert get_tool.annotations.read_only_hint is True
    assert "dry_run" not in props
    post_props = _tool(server, "demo_create_widget").parameters.get("properties") or {}
    assert {"org_id", "mode", "body", "dry_run"} <= set(post_props)
    assert "confirm" not in post_props
    assert _tool(server, "demo_create_widget").annotations.read_only_hint is not True
    assert _tool(server, "demo_delete_widgets").annotations.destructive_hint is True


@pytest.mark.parametrize(
    "name",
    [
        "Accept",
        "Content-Type",
        "Content-Length",
        "Host",
        "Connection",
        "Keep-Alive",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Forwarded",
        "Via",
        "X-Real-IP",
        "Proxy-Authorization",
        "X-Forwarded-For",
        "X-Envoy-External-Address",
    ],
)
def test_transport_headers_are_recognized_case_insensitively(name):
    assert is_transport_header(name)


@pytest.mark.parametrize("name", ["Accept-Language", "If-Match", "Idempotency-Key", "Tenant-Acid", "X-Trace"])
def test_business_headers_are_not_treated_as_transport_headers(name):
    assert not is_transport_header(name)


async def test_read_dispatch_keeps_false_and_drops_unset():
    server, _, rec = _register()
    out = await _tool(server, "demo_list_widgets").fn(
        org_id="o1",
        verbose=False,
        mode="fast",
        site_ids=["site-1", "site-2"],
        if_match='"version-1"',
        tenant_acid="tenant-1",
        idempotency_key="request-1",
    )
    assert out == {"ok": True}
    call = rec.calls[0]
    assert call["method"] == "GET" and call["path"] == "/api/v1/orgs/o1/widgets"
    assert call["kind"] == "read"
    assert call["params"] == {"verbose": False, "mode": "fast", "site_ids": ["site-1", "site-2"]}
    assert call["headers"] == {"If-Match": '"version-1"', "Tenant-Acid": "tenant-1", "Idempotency-Key": "request-1"}
    assert call["path_args"] == {"org_id": "o1"}


def _with_site_ids(style, explode, extra=None):
    data = _manifest_dict()
    params = data["operations"][0]["parameters"]
    next(p for p in params if p["name"] == "site_ids").update(style=style, explode=explode)
    if extra:
        params.append(extra)
    return data


async def test_form_nonexploded_query_array_is_comma_separated():
    server, _, rec = _register(_with_site_ids("form", False))
    await _tool(server, "demo_list_widgets").fn(org_id="o1", verbose=False, site_ids=["site-1", "site-2"])
    assert rec.calls[0]["params"] == {"verbose": False, "site_ids": "site-1,site-2"}


async def test_form_nonexploded_boolean_array_uses_openapi_casing():
    flags = {
        "name": "flags",
        "in": "query",
        "required": False,
        "type": "array",
        "item_type": "boolean",
        "style": "form",
        "explode": False,
    }
    server, _, rec = _register(_with_site_ids("form", True, flags))
    await _tool(server, "demo_list_widgets").fn(org_id="o1", flags=[False, True])
    assert rec.calls[0]["params"]["flags"] == "false,true"


async def test_form_exploded_query_array_retains_repeated_key_values():
    server, _, rec = _register(_with_site_ids("form", True))
    await _tool(server, "demo_list_widgets").fn(org_id="o1", site_ids=["site-1", "site-2"])
    assert rec.calls[0]["params"]["site_ids"] == ["site-1", "site-2"]


def test_array_python_type_preserves_known_item_types():
    assert _py_type("array", "string") == list[str]
    assert _py_type("array", "integer") == list[int]
    assert _py_type("array", "number") == list[float]
    assert _py_type("array", "boolean") == list[bool]
    assert _py_type("array", None) is list
    assert _py_type("array", "any") is list


async def test_invalid_enum_is_refused_before_sending():
    server, _, rec = _register()
    assert await _tool(server, "demo_list_widgets").fn(org_id="o1", mode="turbo") == {
        "error": "'mode' must be one of: 'fast', 'slow'"
    }
    assert await _tool(server, "demo_create_widget").fn(org_id="o1", mode="turbo", body={"a": "x"}) == {
        "error": "'mode' must be one of: 'fast', 'slow'"
    }
    assert rec.calls == []


async def test_incompatible_enum_metadata_keeps_declared_parameter_type():
    legacy = {
        "name": "legacy-flag",
        "in": "query",
        "required": False,
        "type": "boolean",
        "enum": ["false", "true"],
        "default": False,
    }
    server, _, rec = _register(_with_site_ids(None, None, legacy))
    tool = _tool(server, "demo_list_widgets")
    schema = (tool.parameters.get("properties") or {})["legacy_flag"]
    variants = schema.get("anyOf", [schema])
    assert any(v.get("type") == "boolean" for v in variants)
    assert not any("enum" in v for v in variants)
    await tool.fn(org_id="o1", legacy_flag=False)
    assert rec.calls[0]["params"]["legacy-flag"] is False


async def test_large_enum_is_enforced_without_expanding_tool_schema():
    choices = [f"choice-{i}" for i in range(21)]
    large = {"name": "large-mode", "in": "query", "required": False, "type": "string", "enum": choices}
    server, _, rec = _register(_with_site_ids(None, None, large))
    tool = _tool(server, "demo_list_widgets")
    schema = (tool.parameters.get("properties") or {})["large_mode"]
    assert not any("enum" in v for v in schema.get("anyOf", [schema]))
    out = await tool.fn(org_id="o1", large_mode="invalid")
    assert out["error"].startswith("'large-mode' must be one of: 'choice-0', 'choice-1'")
    assert out["error"].endswith(", ... (21 total)")
    assert rec.calls == []


async def test_read_post_has_a_body_and_no_dry_run():
    server, _, rec = _register(kind_of=lambda method, path, op_id: "read" if method != "DELETE" else "delete")
    tool = _tool(server, "demo_create_widget")
    props = tool.parameters.get("properties") or {}
    assert tool.annotations.read_only_hint is True
    assert "body" in (tool.parameters.get("required") or [])
    assert "dry_run" not in props and "confirm" not in props
    await tool.fn(org_id="o1", body={"name": "leaf"})
    assert rec.calls[0]["method"] == "POST" and rec.calls[0]["json"] == {"name": "leaf"}
    assert rec.calls[0]["content_type"] == "application/json"


async def test_path_escaping_and_traversal_refusal():
    server, _, rec = _register()
    fn = _tool(server, "demo_list_widgets").fn
    await fn(org_id="a b")
    assert rec.calls[0]["path"] == "/api/v1/orgs/a%20b/widgets"
    for bad in ("a/b", "..", "a?x", "a#x", "a%2Fb", "a\\b"):
        out = await fn(org_id=bad)
        assert "error" in out and "Nothing was sent" in out["error"], bad
    assert len(rec.calls) == 1


async def test_write_dispatch_passes_body_and_kind():
    server, _, rec = _register()
    out = await _tool(server, "demo_create_widget").fn(org_id="o1", body={"a": "x"})
    assert out == {"ok": True}
    assert rec.calls[0]["method"] == "POST" and rec.calls[0]["json"] == {"a": "x"} and rec.calls[0]["kind"] == "config"


async def test_dry_run_hides_secrets_and_sends_nothing():
    server, _, rec = _register()
    out = await _tool(server, "demo_create_widget").fn(org_id="o1", body={"a": "x", "psk": "s3cret"}, dry_run=True)
    assert out["would_send"]["body"] == {"a": "x", "psk": "[hidden]"}
    assert rec.calls == []


async def test_a_list_reply_is_paged():
    class ListReplies(Recorder):
        async def request(self, method, path, **kwargs):
            return [{"id": i} for i in range(500)]

    data = _manifest_dict()
    m = Manifest(product="demo", base_path="", source=data["source"], operations=data["operations"])
    server = MCPServer("demo-list")
    register_generated_tools(server, m, client=ListReplies)
    out = await _tool(server, "demo_list_widgets").fn(org_id="o1")
    assert len(out["items"]) == 50 and out["_pagination"]["total"] == 500
