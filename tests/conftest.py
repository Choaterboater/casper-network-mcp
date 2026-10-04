"""Shared fixtures: an in-process fake of each product API that records every request.

The ``Router`` idea (routes plus a call log on ``httpx.MockTransport``) comes
from mist-mcp's ``tests/conftest.py``. Nothing here touches the network.
"""

from __future__ import annotations

import inspect
import json as jsonlib
from typing import Any

import httpx
import pytest

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import kind_for_operation
from casper_network_mcp.openapi_gen.manifest import load_manifest
from casper_network_mcp.openapi_gen.runtime import generated_backend

MIST_BASE = "https://api.mist.com"
CENTRAL_BASE = "https://de1.api.central.arubanetworks.com"
CLEARPASS_BASE = "https://198.51.100.20"
TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"


class RecordingTransport(httpx.MockTransport):
    """A fake product API: replies by (method, path), records every product request.

    Login traffic (the Central token endpoint) is answered but kept out of
    ``calls``, so ``calls == []`` means nothing reached the product.
    """

    def __init__(self) -> None:
        super().__init__(self._handle)
        self.calls: list[httpx.Request] = []
        self.token_calls: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = {}
        self.default: Any = {"ok": True}

    def reply(self, payload: Any, method: str | None = None, path: str | None = None) -> None:
        """Reply ``payload`` to (method, path), or to everything when both are None."""
        if method is None and path is None:
            self.default = payload
        else:
            self.routes[((method or "GET").upper(), path or "/")] = payload

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URL):
            self.token_calls.append(request)
            return httpx.Response(200, json={"access_token": f"tok-{len(self.token_calls)}", "expires_in": 7200})
        self.calls.append(request)
        payload = self.routes.get((request.method, request.url.path), self.default)
        if callable(payload):
            payload = payload(request)
        if isinstance(payload, httpx.Response):
            return payload
        if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], int):
            status, body = payload
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        if isinstance(payload, int):
            return httpx.Response(payload, json={"detail": f"status {payload}"})
        return httpx.Response(200, json=payload)


def body_of(request: httpx.Request) -> Any:
    return jsonlib.loads(request.content) if request.content else None


@pytest.fixture
def recording_transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def mist_client_factory():
    from casper_network_mcp.products.mist.client import MistClient

    def make(*, gate: Gate, transport: httpx.MockTransport, **kw: Any) -> MistClient:
        return MistClient(gate=gate, base_url=MIST_BASE, token="test-token", transport=transport, **kw)

    return make


@pytest.fixture
def central_client_factory():
    from casper_network_mcp.products.central.client import CentralClient

    def make(*, gate: Gate, transport: httpx.MockTransport, **kw: Any) -> CentralClient:
        return CentralClient(
            gate=gate,
            base_url=CENTRAL_BASE,
            client_id="test-id",
            client_secret="test-secret",
            transport=transport,
            **kw,
        )

    return make


@pytest.fixture
def clearpass_client_factory():
    from casper_network_mcp.products.clearpass.client import ClearPassClient

    def make(*, gate: Gate, transport: httpx.MockTransport, **kw: Any) -> ClearPassClient:
        return ClearPassClient(gate=gate, base_url=CLEARPASS_BASE, token="test-token", transport=transport, **kw)

    return make


class Backend:
    """A backend with plain ``call(name, args)`` and ``schema(name)`` helpers for tests."""

    def __init__(self, server: Any) -> None:
        self.server = server

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        return await sdk_compat.call_tool_raw(self.server, name, args)

    def schema(self, name: str) -> dict[str, Any]:
        tool = sdk_compat.get_tool(self.server, name)
        assert tool is not None, name
        return tool.parameters

    def description(self, name: str) -> str:
        tool = sdk_compat.get_tool(self.server, name)
        assert tool is not None, name
        return tool.description or ""


@pytest.fixture
def mist_backend(recording_transport, mist_client_factory) -> Backend:
    """The hand-written Mist backend on the recording fake (writes allowed)."""
    from casper_network_mcp.products.mist import tools as mist

    client = mist_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    return Backend(mist.backend(client=lambda: client))


@pytest.fixture
def generated_backend_factory(mist_client_factory, central_client_factory, clearpass_client_factory):
    factories = {"mist": mist_client_factory, "central": central_client_factory, "clearpass": clearpass_client_factory}

    def make(product: str, *, gate: Gate, transport: httpx.MockTransport) -> Backend:
        client = factories[product](gate=gate, transport=transport)
        return Backend(generated_backend(product, client=lambda: client))

    return make


def first_write_tool(product: str) -> str:
    """The first generated tool (in manifest order) whose kind is ``config``."""
    m = load_manifest(product)
    for op in m.operations:
        if kind_for_operation(op["method"], m.base_path + op["path"], op.get("operation_id", "")) == "config":
            return op["name"]
    raise AssertionError(f"no config tool for {product}")


def placeholder_args(tool: Any = None) -> dict[str, Any]:
    """Fill every required argument of ``tool`` with a harmless placeholder."""
    if tool is None:
        return {}
    args: dict[str, Any] = {}
    for name, param in inspect.signature(tool.fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        ann = param.annotation
        origin = getattr(ann, "__origin__", None)
        if name == "body" or ann is dict:
            args[name] = {}
        elif ann is int:
            args[name] = 1
        elif ann is float:
            args[name] = 1.0
        elif ann is bool:
            args[name] = False
        elif ann is list or origin is list:
            args[name] = []
        elif getattr(ann, "__args__", None) and origin is not list and str(ann).startswith("typing.Literal"):
            args[name] = ann.__args__[0]
        else:
            args[name] = "x1"
    return args
