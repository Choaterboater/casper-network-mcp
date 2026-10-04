"""The gate: the first check on every request, before the path check and before sending.

Every product client's ``request()`` (and Central's ``request_sync()``) calls
:meth:`Gate.check` first. Under ``--read-only`` (Casper's pin, a command-line
flag read once at start) the gate lets through only:

* a GET whose change kind is ``read``;
* a POST on ``READ_POSTS`` (a reviewed POST that only reads);
* a POST on ``TROUBLESHOOT_OPS`` sent as ``troubleshoot`` (a hand-checked
  check such as ping or show that changes nothing).

Everything else is refused before anything is sent. A kind a caller declares
can only make the gate stricter, never looser: a PUT called ``read`` is still
a PUT.

Without the pin, a write passes to the product, whose own role decides, with
one plain check first (:meth:`Gate.check_scope`): when the login's scopes are
known and the write's target is resolved to somewhere nothing in the login's
``can_change`` list covers, it is refused before sending. The gate loads a
product's scopes itself, once, before that product's first write (and
``access_check`` fills the same cache). A failed load, an unknown target or
an empty ``can_change`` list never refuses: the product decides.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.kinds import (
    CHANGE_KINDS,
    READ_POSTS,
    TROUBLESHOOT_OPS,
    kind_for_operation,
    listed_template,
)

__all__ = ["PRODUCT_NAMES", "Gate", "LoginMissing", "Refused", "ScopeLoader", "ScopeMap", "Scopes", "is_write"]

PRODUCT_NAMES = {"central": "Central", "mist": "Mist", "clearpass": "ClearPass"}


class Scopes(Protocol):
    """What one product's login may change, as the gate needs it."""

    async def refusal(self, client: Any, method: str, path: str, args: dict[str, Any]) -> str | None:
        """A plain refusal when the target is resolved and not covered; else None."""
        ...


#: Loads a product's scopes through its client (None when they can't be known).
ScopeLoader = Callable[[Any], Awaitable["Scopes | None"]]

#: Per-product scopes the gate has loaded.
ScopeMap = dict[str, Scopes]


def is_write(method: str, path: str, kind: str | None = None) -> bool:
    """True unless the request only reads or runs a listed check."""
    method = method.upper()
    if method == "GET":
        return kind not in (None, "read") or kind_for_operation(method, path) != "read"
    template = listed_template(method, path)
    if method == "POST" and template is not None:
        if (method, template) in READ_POSTS and kind in (None, "read"):
            return False
        if (method, template) in TROUBLESHOOT_OPS and kind in (None, "troubleshoot"):
            return False
    return True


class Refused(ToolError):
    """The gate refused a request; nothing was sent."""


class LoginMissing(ToolError):
    """A product has no login set up, so nothing can be sent to it."""

    def __init__(self, product: str) -> None:
        self.product = product
        name = PRODUCT_NAMES.get(product, product)
        super().__init__(f"There is no {name} login set up, so nothing was sent to {name}.")

    def as_error(self) -> dict[str, Any]:
        return {"error": "login_missing", "product": self.product}


class Gate:
    """The read-only pin and the login's scopes."""

    def __init__(
        self,
        read_only: bool,
        scopes: ScopeMap | None = None,
        loaders: dict[str, ScopeLoader] | None = None,
    ) -> None:
        self.read_only = bool(read_only)
        self.scopes: ScopeMap = dict(scopes or {})
        self.loaders: dict[str, ScopeLoader] = dict(loaders or {})
        self._locks: dict[str, asyncio.Lock] = {}

    async def scopes_for(self, product: str, client: Any, *, refresh: bool = False) -> Scopes | None:
        """The product's scopes: cached, or loaded once through ``client``.

        A failed load is not cached (the next write tries again) and means
        "no scopes": nothing is refused for it.
        """
        if not refresh and product in self.scopes:
            return self.scopes[product]
        loader = self.loaders.get(product)
        if loader is None:
            return None
        lock = self._locks.setdefault(product, asyncio.Lock())
        async with lock:
            if not refresh and product in self.scopes:
                return self.scopes[product]
            try:
                answer = await loader(client)
            except Exception:  # noqa: BLE001 - a failed load means "the product decides"
                return None
            if answer is not None:
                self.scopes[product] = answer
            return answer

    async def check_scope(
        self, product: str, client: Any, method: str, path: str, args: dict[str, Any], kind: str | None = None
    ) -> None:
        """Refuse a write whose resolved target is outside the login's ``can_change`` list."""
        if not is_write(method, path, kind):
            return
        scopes = await self.scopes_for(product, client)
        if scopes is None:
            return
        message = await scopes.refusal(client, method.upper(), path, dict(args or {}))
        if message:
            raise Refused(message)

    def check(self, product: str, method: str, path: str, args: dict[str, Any], kind: str | None = None) -> None:
        """Raise :class:`Refused` unless ``method path`` may be sent now.

        ``args`` are the call's path values (used by the scope check in Task 12).
        ``kind`` is the caller's change kind; when absent it is worked out from
        the method and path.
        """
        if kind is not None and kind not in CHANGE_KINDS:
            raise ValueError(f"unknown change kind {kind!r}")
        method = method.upper()
        if not self.read_only:
            return
        derived = kind_for_operation(method, path)
        declared = kind if kind is not None else derived
        template = listed_template(method, path)
        if method == "GET" and derived == "read" and declared == "read":
            return
        if method == "POST" and template is not None:
            if (method, template) in READ_POSTS and declared == "read":
                return
            if (method, template) in TROUBLESHOOT_OPS and declared == "troubleshoot":
                return
        name = PRODUCT_NAMES.get(product, product)
        raise Refused(
            f"Not sent: this server is read-only (started with --read-only), so it only reads from {name} "
            f"and runs the listed checks. {method} {path} would change {name}."
        )
