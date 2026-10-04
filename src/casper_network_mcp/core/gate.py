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
a PUT. Without the pin, writes pass to the product, whose own role decides;
the login's ``can_change`` scopes are enforced here in Task 12.
"""

from __future__ import annotations

from typing import Any

from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.kinds import (
    CHANGE_KINDS,
    READ_POSTS,
    TROUBLESHOOT_OPS,
    kind_for_operation,
    listed_template,
)

__all__ = ["PRODUCT_NAMES", "Gate", "LoginMissing", "Refused", "ScopeMap"]

PRODUCT_NAMES = {"central": "Central", "mist": "Mist", "clearpass": "ClearPass"}

#: Per-product login scopes (``can_change`` / ``read_only`` lists); filled in Task 12.
ScopeMap = dict[str, Any]


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
    """The read-only pin (and, from Task 12, the login's scopes)."""

    def __init__(self, read_only: bool, scopes: ScopeMap | None = None) -> None:
        self.read_only = bool(read_only)
        self.scopes: ScopeMap = dict(scopes or {})

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
