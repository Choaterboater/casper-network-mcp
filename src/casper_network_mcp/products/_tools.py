"""What every hand-written tool shares: its label, its client and its error shape.

* :class:`ToolSet` collects a module's tools. The ``@tools.tool(...)`` decorator
  keeps the copied modules' shape, but the label it is given is ignored: each
  tool's annotation and ``_meta["casper/change-kind"]`` come only from its
  product's ``labels.yaml`` row (checked by hand), so a label can't drift.
* :func:`use_client` / :func:`client` hold the one client getter per product
  that the tools call at call time (a missing login shows up when a tool runs,
  not when the server starts). Until the server sets one, the getter builds a
  client from the login variables behind a read-only gate, so nothing changes
  by accident.
* :func:`guarded` turns a refusal, a missing login, an API error, a network
  failure, a bad argument or a failed step the copied code reports with
  ``RuntimeError`` into ``{"error": ...}`` instead of a stack trace.
* :func:`would_send` is the opt-in preview every change tool returns when
  called with ``dry_run=True``; nothing is sent.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.gate import PRODUCT_NAMES, Gate
from casper_network_mcp.core.kinds import tool_labels
from casper_network_mcp.core.redact import redact_sensitive, redact_tool_error_text

__all__ = ["ClientGetter", "ToolSet", "client", "guarded", "unknown_body_nodes", "use_client", "would_send"]

ClientGetter = Callable[[], Any]

_getters: dict[str, ClientGetter] = {}
_defaults: dict[str, Any] = {}


def _default_client(product: str) -> Any:
    """A client from the login variables behind a read-only gate (used only if the server set none)."""
    if product not in _defaults:
        if product == "mist":
            from casper_network_mcp.products.mist.client import MistClient

            _defaults[product] = MistClient.from_env(Gate(read_only=True))
        elif product == "central":
            from casper_network_mcp.products.central.client import CentralClient

            _defaults[product] = CentralClient.from_env(Gate(read_only=True))
        elif product == "clearpass":
            from casper_network_mcp.products.clearpass.client import ClearPassClient

            _defaults[product] = ClearPassClient.from_env(Gate(read_only=True))
        else:
            raise KeyError(product)
    return _defaults[product]


def use_client(product: str, getter: ClientGetter) -> None:
    """Make ``getter`` the client every ``product`` tool calls from now on."""
    _getters[product] = getter


def client(product: str) -> Any:
    """The current client for ``product``."""
    getter = _getters.get(product)
    return getter() if getter is not None else _default_client(product)


def _as_error(product: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ToolError):
        return exc.as_error()
    if isinstance(exc, httpx.HTTPError):
        name = PRODUCT_NAMES.get(product, product)
        return {"error": redact_tool_error_text(f"Could not reach {name}: {exc}")}
    return {"error": redact_tool_error_text(str(exc))}


_HANDLED = (ToolError, httpx.HTTPError, ValueError, RuntimeError)


def guarded(product: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so expected failures come back as ``{"error": ...}``."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except _HANDLED as exc:
                return _as_error(product, exc)

        return run_async

    @functools.wraps(fn)
    def run_sync(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _HANDLED as exc:
            return _as_error(product, exc)

    return run_sync


def would_send(method: str, path: str, body: Any = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The preview a change tool returns with ``dry_run=True``; secret values hidden."""
    preview: dict[str, Any] = {"method": method.upper(), "path": path, "body": redact_sensitive(body)}
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    if clean:
        preview["params"] = redact_sensitive(clean)
    return {"would_send": preview}


def unknown_body_nodes(product: str, method: str, path: str, body: Any) -> list[str]:
    """Top-level body keys the bundled spec does not declare for that operation.

    A dry run calls this so a payload Central would reject is named before anything
    is sent — for instance ``version-chart`` on ``POST /network-config/v1alpha1/device-firmware``,
    whose schema declares only ``issu`` and ``site-distribution``. Empty when the
    bundled documents describe no request body for the operation.
    """
    if not isinstance(body, dict) or not body:
        return []
    from casper_network_mcp import specs_bundle

    declared = specs_bundle.request_body_properties(product, method, path)
    if declared is None:
        return []
    return sorted(str(key) for key in body if key not in declared)


class ToolSet:
    """The tools one module defines, registered on a backend with their labels."""

    def __init__(self, product: str) -> None:
        self.product = product
        self.functions: list[Callable[..., Any]] = []

    def tool(self, *args: Any, **_ignored: Any) -> Any:
        """Collect a tool. Any ``annotations=`` given is ignored: labels come from ``labels.yaml``."""

        def collect(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.functions.append(fn)
            return fn

        if args and callable(args[0]):
            return collect(args[0])
        return collect

    def register(self, server: MCPServer) -> list[str]:
        names: list[str] = []
        for fn in self.functions:
            annotations, meta = tool_labels(self.product, fn.__name__)
            server.add_tool(guarded(self.product, fn), name=fn.__name__, annotations=annotations, meta=meta)
            names.append(fn.__name__)
        return names
