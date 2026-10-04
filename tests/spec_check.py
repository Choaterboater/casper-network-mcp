"""Call every hand-written tool against a recording fake; match each request to the bundled spec.

The idea comes from mist-mcp's ``tests/spec_check.py``: the old tests there
checked the code against its own guessed paths, so a third of its tools
called endpoints Mist doesn't have and every test still passed. Here the
operations come from the vendor documents bundled in the package
(``specs_bundle.operations``), for all three products.

* :func:`calls_for` -- placeholder arguments, the same with every optional
  argument filled, plus one call per value of each
  ``Literal`` argument, plus every branch listed in ``spec_branches.yaml``.
* :func:`record_requests` -- run the tool on a fake product API (writes
  allowed, nothing leaves the process) and return what it sent.
* :func:`match` -- the operation a request lands on. Fixed segments must
  match exactly; a ``{param}`` segment takes a value only when no operation
  for the same method has that value as a fixed segment at that place.
* :func:`problems` -- what is wrong with one request: no operation, a query
  name the operation doesn't have, a required query left out, or a value
  outside the spec's enum.
"""

from __future__ import annotations

import functools
import inspect
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import yaml  # type: ignore[import-untyped]
from conftest import CENTRAL_BASE, CLEARPASS_BASE, MIST_BASE, RecordingTransport, _placeholder, placeholder_args

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.openapi_gen.manifest import load_manifest
from casper_network_mcp.products import _tools, hand_written_backends
from casper_network_mcp.specs_bundle import Operation, operations

__all__ = [
    "NOT_CHECKED",
    "VALIDATE_FIRST",
    "HandTool",
    "all_hand_written_tools",
    "calls_for",
    "match",
    "problems",
    "record_requests",
]

BRANCHES_FILE = Path(__file__).with_name("spec_branches.yaml")

#: Read passthroughs: the caller names the path, so there is no fixed endpoint to check.
NOT_CHECKED: dict[str, str] = {
    "central_get": "read passthrough: the caller gives the path (GET only, gated)",
    "mist_get": "read passthrough: the caller gives the path (GET only, gated)",
    "clearpass_get": "read passthrough: the caller gives the path (GET only, gated)",
}


@functools.cache
def _branches() -> dict[str, Any]:
    data = yaml.safe_load(BRANCHES_FILE.read_text(encoding="utf-8")) or {}
    return data


#: Tools that send nothing for placeholder arguments: they check input first, or
#: answer from built-in notes. Each has a reason in ``spec_branches.yaml``.
VALIDATE_FIRST: dict[str, str] = dict(_branches().get("validate_first") or {})


@dataclass
class HandTool:
    name: str
    product: str
    server: Any
    fn: Any
    parameters: dict[str, Any] = field(default_factory=dict)


@functools.cache
def _tools_by_name() -> dict[str, HandTool]:
    out: dict[str, HandTool] = {}
    for product, server in hand_written_backends():
        for name, tool in sdk_compat.tool_registry(server).items():
            out[name] = HandTool(name, product, server, tool.fn, tool.parameters)
    return out


def all_hand_written_tools() -> list[HandTool]:
    return sorted(_tools_by_name().values(), key=lambda t: t.name)


# ── calls ──────────────────────────────────────────────────────────────────


def _literal_values(ann: Any) -> tuple[Any, ...]:
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    if origin is typing.Literal:
        return args
    if origin in (typing.Union, types.UnionType):
        for a in args:
            found = _literal_values(a)
            if found:
                return found
    return ()


#: A documentation MAC (RFC 7042) for arguments that must look like one.
MAC = "00:00:5e:00:53:01"


def _better(name: str, value: Any, hint: Any = None) -> Any:
    """A placeholder that passes the tool's own input checks, so the request is really sent."""
    lowered = name.lower()
    if isinstance(value, list) and not value:
        inner = [a for a in typing.get_args(hint) if a is not type(None)]
        while inner and typing.get_origin(inner[0]) in (typing.Union, types.UnionType, list):
            inner = [a for a in typing.get_args(inner[0]) if a is not type(None)]
        element = inner[0] if inner else str
        if element is int:
            return [1]
        if element is dict or typing.get_origin(element) is dict:
            return [{}]
    if "mac" in lowered:
        return [MAC] if isinstance(value, list) else MAC
    if lowered.endswith("scope_id") or lowered == "scope_id":
        return "1001"
    if lowered == "commands":
        return ["show version"]
    if isinstance(value, list) and not value:
        return ["x1"]
    return value


def calls_for(tool: HandTool) -> list[dict[str, Any]]:
    """Placeholder arguments, the same with every optional argument filled,
    one call per ``Literal`` value, and the listed branches."""
    try:
        hints = typing.get_type_hints(inspect.unwrap(tool.fn))
    except Exception:  # noqa: BLE001 - an unresolvable hint just means no enum branches
        hints = {}
    base = {name: _better(name, value, hints.get(name)) for name, value in placeholder_args(tool).items()}
    extra = _branches().get("defaults", {}).get(tool.name) or {}
    base = {**base, **extra}
    calls = [base]
    # One more call with every optional argument that defaults to None filled
    # in, so optional query parameters are checked too.
    optional = {
        name: _better(name, _placeholder(hints.get(name, param.annotation), name), hints.get(name))
        for name, param in inspect.signature(tool.fn).parameters.items()
        if param.default is None and name not in base
    }
    if optional:
        calls.append({**optional, **base})
    for name in inspect.signature(tool.fn).parameters:
        for value in _literal_values(hints.get(name)):
            if base.get(name) != value:
                calls.append({**base, name: value})
    for branch in _branches().get("branches", {}).get(tool.name) or []:
        calls.append({**base, **branch})
    return calls


# ── recording ──────────────────────────────────────────────────────────────


def _client(product: str, transport: httpx.MockTransport) -> Any:
    gate = Gate(read_only=False)
    if product == "mist":
        from casper_network_mcp.products.mist.client import MistClient

        return MistClient(gate=gate, base_url=MIST_BASE, token="test-token", transport=transport)
    if product == "central":
        from casper_network_mcp.products.central.client import CentralClient

        return CentralClient(
            gate=gate, base_url=CENTRAL_BASE, client_id="test-id", client_secret="test-secret", transport=transport
        )
    from casper_network_mcp.products.clearpass.client import ClearPassClient

    return ClearPassClient(gate=gate, base_url=CLEARPASS_BASE, token="test-token", transport=transport)


@dataclass
class Sent:
    method: str
    path: str
    params: list[tuple[str, str]]


async def record_requests(tool: HandTool, args: dict[str, Any], *, failing: bool = False) -> list[Sent]:
    """Run ``tool`` once on a fake product API and return every request it sent.

    With ``failing=True`` every unlisted request is answered 404, so a tool
    that tries another path after an error shows that path too.
    """
    from casper_network_mcp.products.central import compat

    transport = RecordingTransport()
    for spec in _branches().get("replies", {}).get(tool.name) or []:
        transport.reply(spec["reply"], spec.get("method", "GET"), spec["path"])
    transport.reply(404 if failing else _branches().get("default_reply", {"items": [], "count": 0}))
    client = _client(tool.product, transport)
    previous = _tools._getters.get(tool.product)
    saved = (compat.POLL_INTERVAL, compat.POLL_MAX)
    compat.POLL_INTERVAL, compat.POLL_MAX = 0, 1
    _tools.use_client(tool.product, lambda: client)
    try:
        try:
            await sdk_compat.call_tool_raw(tool.server, tool.name, args)
        except Exception:  # noqa: BLE001, S110 - a refusal or bad placeholder still shows what was sent
            pass
    finally:
        compat.POLL_INTERVAL, compat.POLL_MAX = saved
        if previous is not None:
            _tools.use_client(tool.product, previous)
        else:
            _tools._getters.pop(tool.product, None)
    return [
        Sent(r.method, r.url.raw_path.decode().split("?")[0], list(r.url.params.multi_items())) for r in transport.calls
    ]


# ── matching ───────────────────────────────────────────────────────────────


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


@functools.cache
def _table(product: str) -> dict[str, list[tuple[tuple[str, ...], Operation]]]:
    table: dict[str, list[tuple[tuple[str, ...], Operation]]] = {}
    for op in operations(product):
        segments = tuple(s for s in op.path.split("/") if s)
        table.setdefault(op.method, []).append((segments, op))
    return table


def _strip_base(product: str, path: str) -> str | None:
    base = load_manifest(product).base_path
    if not base:
        return path
    if path == base or path.startswith(base + "/"):
        return path[len(base) :] or "/"
    return None


def _walk(product: str, method: str, path: str) -> tuple[Operation, dict[str, str]] | None:
    stripped = _strip_base(product, path)
    if stripped is None:
        return None
    segments = [unquote(s) for s in stripped.split("/") if s]
    candidates = [(segs, op) for segs, op in _table(product).get(method.upper(), []) if len(segs) == len(segments)]
    for i, value in enumerate(segments):
        fixed = [(segs, op) for segs, op in candidates if segs[i] == value]
        candidates = fixed if fixed else [(segs, op) for segs, op in candidates if _is_param(segs[i])]
        if not candidates:
            return None
    segs, op = candidates[0]
    captured = {s[1:-1]: v for s, v in zip(segs, segments, strict=True) if _is_param(s)}
    return op, captured


def match(method: str, path: str, product: str) -> Operation | None:
    """The bundled operation ``method path`` lands on, or None."""
    found = _walk(product, method, path)
    return found[0] if found else None


def _in_enum(value: str, allowed: tuple[str, ...]) -> bool:
    if value in allowed:
        return True
    parts = [p.strip() for p in value.split(",")]
    return len(parts) > 1 and all(p in allowed for p in parts)


def problems(tool: HandTool, req: Sent) -> list[str]:
    """What is wrong with one request (empty when it matches the spec)."""
    found = _walk(tool.product, req.method, req.path)
    if found is None:
        return [f"{req.method} {req.path} is not in the {tool.product} spec"]
    op, captured = found
    out: list[str] = []
    sent = {k for k, _ in req.params}
    unknown = sorted(sent - set(op.query_params))
    if unknown:
        out.append(f"{req.method} {op.path}: unknown query {unknown}")
    missing = sorted(set(op.required_query) - sent)
    if missing:
        out.append(f"{req.method} {op.path}: required query {missing} not sent")
    for name, value in [*req.params, *captured.items()]:
        allowed = op.enums.get(name)
        if allowed and not _in_enum(value, allowed):
            out.append(f"{req.method} {op.path}: {name}={value!r} is not one of {list(allowed)}")
    return out
