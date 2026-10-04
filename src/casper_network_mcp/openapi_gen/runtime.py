"""Register the operations in a manifest as MCP tools.

Adapted from hpe-networking-mcp ``openapi_gen/runtime.py`` (MIT,
nowireless4u/hpe-networking-mcp). Each operation becomes one tool with a
typed signature from its path, query and header parameters, plus ``body``
when the operation takes one. What changed from the source:

* no ``confirm`` argument and no "refuse without confirm=True" step: approval
  is Casper's box, never an argument the AI sets;
* ``dry_run`` exists only on tools that change something, defaults to
  ``False``, and when ``True`` returns ``{"would_send": ...}`` without calling
  the product at all;
* no env switch turns generated tools on or off, and no write gate lives
  here: every call goes through the product client's gated ``request()``
  (:mod:`.http_exec`);
* path pieces go through :func:`core.paths.path_segment`, which refuses
  ``/``, ``?``, ``#``, ``%`` and ``..`` before anything is sent.
"""

from __future__ import annotations

import inspect
import keyword
import re
from collections.abc import Callable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.kinds import kind_for_operation, label_for_kind, tool_meta
from casper_network_mcp.core.paths import UnsafePath, path_segment
from casper_network_mcp.core.redact import redact_sensitive
from casper_network_mcp.openapi_gen.http_exec import ProductClient, send
from casper_network_mcp.openapi_gen.manifest import Manifest, load_manifest
from casper_network_mcp.openapi_gen.naming import digest, snake

__all__ = ["generated_backend", "is_auth_param", "is_transport_header", "register_generated_tools"]

KindOf = Callable[[str, str, str], str]
ClientGetter = Callable[[], ProductClient]

_PATH_PLACEHOLDER = re.compile(r"\{([^}]+)\}")

# Header/cookie parameter names that carry credentials never become arguments;
# the product client adds the login itself.
_AUTH_PARAM_NAMES = {
    "authorization",
    "cookie",
    "x-csrftoken",
    "x-csrf-token",
    "apitoken",
    "api-token",
    "x-api-token",
    "x-api-key",
    "apikey",
    "api-key",
    "token",
    "x-auth-token",
}

# The HTTP client owns framing, routing and source-identity headers.
_TRANSPORT_HEADER_NAMES = {
    "accept",
    "content-type",
    "content-length",
    "host",
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "forwarded",
    "via",
    "x-real-ip",
}
_TRANSPORT_HEADER_PREFIXES = ("proxy-", "x-forwarded-", "x-envoy-")

# Arguments the runtime adds; a spec parameter with one of these names is renamed.
_RESERVED_ARG_NAMES = frozenset({"body", "dry_run"})

_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    "any": Any,
}

_MAX_DESC = 200
_MAX_ENUM_ERROR_CHOICES = 20
_MAX_ENUM_LITERAL_VALUES = 20


class _ParamSpec:
    """How one tool argument maps to one API parameter."""

    __slots__ = ("api", "arg", "default", "description", "enum", "explode", "location", "py_type", "required", "style")

    def __init__(
        self,
        arg: str,
        api: str,
        location: str,
        py_type: Any,
        required: bool,
        default: Any,
        description: str,
        enum: list[Any] | None,
        style: str | None,
        explode: bool | None,
    ) -> None:
        self.arg = arg
        self.api = api
        self.location = location
        self.py_type = py_type
        self.required = required
        self.default = default
        self.description = description
        self.enum = enum
        self.style = style
        self.explode = explode


def is_auth_param(name: str) -> bool:
    return name.strip().lower() in _AUTH_PARAM_NAMES


def is_transport_header(name: str) -> bool:
    normalized = name.strip().lower()
    return normalized in _TRANSPORT_HEADER_NAMES or normalized.startswith(_TRANSPORT_HEADER_PREFIXES)


def _safe_arg_name(name: str, taken: set[str]) -> str:
    arg = snake(name) or "arg"
    if arg[0].isdigit():
        arg = f"p_{arg}"
    if keyword.iskeyword(arg):
        arg = f"{arg}_"
    base = arg
    i = 2
    while arg in taken:
        arg = f"{base}_{i}"
        i += 1
    taken.add(arg)
    return arg


def _py_type(schema_type: str, item_type: str | None = None) -> Any:
    if schema_type == "array":
        elem = _PY_TYPES.get(item_type or "any", Any)
        return list if elem is Any else list[elem]  # type: ignore[valid-type]
    return _PY_TYPES.get(schema_type, Any)


def _compatible_enum_values(spec: _ParamSpec) -> tuple[Any, ...]:
    values = tuple(spec.enum or ())
    if not values:
        return ()
    if spec.py_type is str:
        compatible = all(type(value) is str for value in values)
    elif spec.py_type is bool:
        compatible = all(type(value) is bool for value in values)
    elif spec.py_type is int:
        compatible = all(type(value) is int for value in values)
    elif spec.py_type is float:
        compatible = all(type(value) in {int, float} for value in values)
    elif spec.py_type is Any:
        compatible = all(value is None or type(value) in {str, bool, int, float} for value in values)
    else:
        compatible = False
    return values if compatible else ()


def _parameter_type(spec: _ParamSpec) -> Any:
    enum_values = _compatible_enum_values(spec)
    if enum_values and len(enum_values) <= _MAX_ENUM_LITERAL_VALUES:
        return Literal[enum_values]  # type: ignore[valid-type]
    return spec.py_type


def _enum_value_allowed(spec: _ParamSpec, value: Any, enum_values: tuple[Any, ...]) -> bool:
    if spec.py_type is str:
        return type(value) is str and value in enum_values
    if spec.py_type is bool:
        return type(value) is bool and value in enum_values
    if spec.py_type is int:
        return type(value) is int and value in enum_values
    if spec.py_type is float:
        return type(value) in {int, float} and value in enum_values
    return any(type(value) is type(choice) and value == choice for choice in enum_values)


def _validate_enum_params(specs: list[_ParamSpec], kwargs: dict[str, Any]) -> str | None:
    for spec in specs:
        value = kwargs.get(spec.arg)
        enum_values = _compatible_enum_values(spec)
        if value is None or not enum_values or _enum_value_allowed(spec, value, enum_values):
            continue
        shown = ", ".join(repr(choice) for choice in enum_values[:_MAX_ENUM_ERROR_CHOICES])
        if len(enum_values) > _MAX_ENUM_ERROR_CHOICES:
            shown += f", ... ({len(enum_values)} total)"
        return f"{spec.api!r} must be one of: {shown}"
    return None


def _param_specs(op: dict[str, Any]) -> list[_ParamSpec]:
    specs: list[_ParamSpec] = []
    taken: set[str] = set(_RESERVED_ARG_NAMES)
    for raw in op.get("parameters", []):
        location = raw.get("in")
        if location not in ("path", "query", "header"):
            continue  # cookies are the login's business
        name = raw.get("name", "")
        if location == "header" and (is_auth_param(name) or is_transport_header(name)):
            continue
        arg = _safe_arg_name(name, taken)
        specs.append(
            _ParamSpec(
                arg=arg,
                api=name,
                location=location,
                py_type=_py_type(raw.get("type", "any"), raw.get("item_type")),
                required=bool(raw.get("required", location == "path")),
                default=raw.get("default"),
                description=str(raw.get("description", "")),
                enum=raw.get("enum"),
                style=raw.get("style"),
                explode=raw.get("explode"),
            )
        )
    return specs


def _substitute_path(template: str, path_values: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = path_values.get(key)
        if value is None:
            raise UnsafePath(f"{key} is required")
        try:
            return path_segment(str(value))
        except UnsafePath as exc:
            raise UnsafePath(f"{key}: {exc}. Nothing was sent.") from None

    return _PATH_PLACEHOLDER.sub(repl, template)


def _build_query(specs: list[_ParamSpec], kwargs: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    for spec in specs:
        if spec.location != "query":
            continue
        value = kwargs.get(spec.arg)
        if value is None:  # unset is left out; False, 0 and [] are kept
            continue
        if spec.style == "form" and spec.explode is False and isinstance(value, list):
            query[spec.api] = ",".join(str(item).lower() if isinstance(item, bool) else str(item) for item in value)
        else:
            query[spec.api] = value
    return query


def _build_headers(specs: list[_ParamSpec], kwargs: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for spec in specs:
        if spec.location != "header" or is_auth_param(spec.api) or is_transport_header(spec.api):
            continue
        value = kwargs.get(spec.arg)
        if value is not None:
            headers[spec.api] = str(value)
    return headers


def _path_values(specs: list[_ParamSpec], kwargs: dict[str, Any]) -> dict[str, Any]:
    return {spec.api: kwargs.get(spec.arg) for spec in specs if spec.location == "path"}


def _build_signature(
    specs: list[_ParamSpec],
    *,
    include_body: bool,
    include_dry_run: bool,
    body_type: Any,
    body_required: bool,
) -> tuple[inspect.Signature, dict[str, Any]]:
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    ordered = sorted(specs, key=lambda s: (not s.required, s.location != "path"))
    for spec in ordered:
        param_type = _parameter_type(spec)
        if spec.required:
            annotation, default = param_type, inspect.Parameter.empty
        else:
            annotation, default = (param_type | None if param_type is not Any else Any), None
        annotations[spec.arg] = annotation
        parameters.append(
            inspect.Parameter(spec.arg, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)
        )
    if include_body:
        if body_required:
            annotations["body"] = body_type
            parameters.append(inspect.Parameter("body", inspect.Parameter.KEYWORD_ONLY, annotation=body_type))
        else:
            annotations["body"] = body_type | None if body_type is not Any else Any
            parameters.append(
                inspect.Parameter("body", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=annotations["body"])
            )
    if include_dry_run:
        annotations["dry_run"] = bool
        parameters.append(inspect.Parameter("dry_run", inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool))
    annotations["return"] = dict[str, Any]
    return inspect.Signature(parameters), annotations


def _docstring(op: dict[str, Any], product: str, kind: str) -> str:
    action = op.get("summary") or op.get("operation_id") or op["name"]
    lines = [f"{action} ({op['method']} {op['path']})."]
    if op.get("description") and op["description"] != op.get("summary"):
        lines += ["", op["description"][:400]]
    if op.get("deprecated"):
        lines += ["", "The vendor marks this operation as deprecated."]
    if op.get("sunset"):
        lines += ["", f"Sunset: {op['sunset']}."]
    if kind != "read":
        lines += ["", f"This changes {product}. Set dry_run=true to see the request without sending it."]
    return "\n".join(lines)


def _short_description(op: dict[str, Any]) -> str:
    text = op.get("summary") or op.get("description") or op.get("operation_id") or op["name"]
    text = " ".join(str(text).split())
    if len(text) > _MAX_DESC:
        text = text[: _MAX_DESC - 1].rstrip() + "…"
    lifecycle = " [DEPRECATED]" if op.get("deprecated") else ""
    return f"[{op['method']}]{lifecycle} {text}"


def _make_tool(op: dict[str, Any], manifest: Manifest, client: ClientGetter, kind: str) -> Callable[..., Any]:
    specs = _param_specs(op)
    method = op["method"]
    template = op["path"]
    rb = op.get("request_body") or {}
    content_type = rb.get("content_type", "application/json")
    body_type = _py_type(rb.get("schema_type", "object"), rb.get("item_type"))
    if body_type is Any:
        body_type = dict
    changes = kind != "read"
    product = manifest.product
    base_path = manifest.base_path

    async def _tool(**kwargs: Any) -> dict[str, Any]:
        enum_error = _validate_enum_params(specs, kwargs)
        if enum_error is not None:
            return {"error": enum_error}
        path_args = _path_values(specs, kwargs)
        try:
            path = base_path + _substitute_path(template, path_args)
        except UnsafePath as exc:
            return {"error": str(exc)}
        query = _build_query(specs, kwargs)
        headers = _build_headers(specs, kwargs)
        body = kwargs.get("body")
        if changes and kwargs.get("dry_run"):
            return {
                "would_send": {
                    "method": method,
                    "path": path,
                    "params": redact_sensitive(query),
                    "body": redact_sensitive(body),
                    "content_type": content_type,
                }
            }
        return await send(
            client,
            product=product,
            method=method,
            path=path,
            query=query,
            headers=headers,
            body=body,
            content_type=content_type,
            kind=kind,
            path_args=path_args,
        )

    signature, annotations = _build_signature(
        specs,
        include_body=bool(rb),
        include_dry_run=changes,
        body_type=body_type,
        body_required=bool(rb.get("required", False)),
    )
    _tool.__name__ = op["name"]
    _tool.__qualname__ = op["name"]
    _tool.__doc__ = _docstring(op, product, kind)
    _tool.__signature__ = signature  # type: ignore[attr-defined]
    _tool.__annotations__ = annotations
    return _tool


def register_generated_tools(
    mcp: MCPServer,
    manifest: Manifest,
    *,
    client: ClientGetter,
    kind_of: KindOf = kind_for_operation,
) -> list[str]:
    """Register every operation in ``manifest`` on ``mcp``; return the tool names.

    ``client`` is called at call time (so a missing login is reported when a
    tool is used, not when the server starts). Each tool's change kind comes
    from ``kind_of`` (``core.kinds.kind_for_operation``) on the full request
    path, and sets both its annotation and ``_meta["casper/change-kind"]``.
    """
    existing = set(sdk_compat.tool_names(mcp))
    registered: list[str] = []
    for op in manifest.operations:
        name = op["name"]
        if name in existing:
            name = f"{name}_g{digest(op['method'], op['path'])}"
            if name in existing:
                raise RuntimeError(f"generated tool collision for {name!r}")
            op = {**op, "name": name}
        kind = kind_of(op["method"], manifest.base_path + op["path"], str(op.get("operation_id") or ""))
        mcp.add_tool(
            _make_tool(op, manifest, client, kind),
            name=name,
            description=_short_description(op),
            annotations=label_for_kind(kind),
            meta=tool_meta(kind),
        )
        existing.add(name)
        registered.append(name)
    return registered


def generated_backend(product: str, *, client: ClientGetter) -> MCPServer:
    """A ``<product>-generated`` backend holding one tool per bundled operation."""
    server = MCPServer(f"{product}-generated")
    register_generated_tools(server, load_manifest(product), client=client)
    return server
