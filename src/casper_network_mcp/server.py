"""The server: four top-level tools over every Central, Mist and ClearPass tool.

* ``find_tool`` finds an inner tool and says what kind of change it makes.
* ``invoke_read_tool`` runs an inner tool that only reads (or runs a check).
* ``invoke_tool`` runs any inner tool; the product's gated client decides
  whether a change is sent.
* ``access_check`` says what each login can do.

``--read-only`` is read once, at start; nothing turns writes on while the
server runs. Logins come only from the environment Casper sets.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from casper_network_mcp import __version__, sdk_compat
from casper_network_mcp.core.annotations import READ_ONLY, WRITE
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.core.serve import DEFAULT_HTTP_PORT, TRANSPORTS, run_server
from casper_network_mcp.products import _tools
from casper_network_mcp.router import dispatch, find

__all__ = ["RouterServer", "build_server", "main"]

INSTRUCTIONS = (
    "Network tools for HPE Aruba Central, Juniper Mist and HPE Aruba ClearPass. "
    "Call find_tool with what you want to do, then invoke_read_tool for reads and checks, "
    "or invoke_tool for changes. access_check says what each login can do."
)


class RouterServer(MCPServer):
    """The MCP server, plus ``call(name, args)`` for in-process use and tests."""

    def __init__(self, *, gate: Gate, clients: dict[str, Any]) -> None:
        super().__init__("casper-network-mcp", instructions=INSTRUCTIONS, version=__version__)
        self.gate = gate
        self.clients = clients

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await sdk_compat.call_tool_raw(self, name, dict(arguments or {}))


def _clients(gate: Gate, logins: Mapping[str, str], transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    from casper_network_mcp.products.central.client import CentralClient
    from casper_network_mcp.products.clearpass.client import ClearPassClient
    from casper_network_mcp.products.mist.client import MistClient

    kw: dict[str, Any] = {"transport": transport} if transport is not None else {}
    return {
        "central": CentralClient.from_env(gate, logins=logins, **kw),
        "mist": MistClient.from_env(gate, logins=logins, **kw),
        "clearpass": ClearPassClient.from_env(gate, logins=logins, **kw),
    }


def _fixed(value: Any) -> _tools.ClientGetter:
    return lambda: value


def build_server(
    read_only: bool = False,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> RouterServer:
    """The router server. ``transport`` and ``environ`` are for tests (a fake API, set logins)."""
    gate = Gate(read_only=read_only)
    clients = _clients(gate, read_logins(environ), transport)
    for product, product_client in clients.items():
        _tools.use_client(product, _fixed(product_client))
    server = RouterServer(gate=gate, clients=clients)

    def find_tool(
        query: str, top_k: int = 5, product: str | None = None, include_schema: bool = False
    ) -> list[dict[str, Any]]:
        """Find the tool for a task. Call this first.

        query: what you want to do, in plain words ("bounce port 7 on the
        closet switch") or an exact API call ("GET /api/v1/sites/{site_id}").
        product: central, mist, clearpass or specs (the API document lookup).
        Each hit has name, product, summary, kind (read, troubleshoot, config,
        disruptive, firmware, delete or admin) and label. Pass include_schema
        to get the arguments a tool takes. Then call invoke_read_tool (kind
        read or troubleshoot) or invoke_tool.
        """
        return find.find_tool(query, top_k=top_k, product=product, include_schema=include_schema)

    async def invoke_read_tool(name: str, arguments: dict[str, Any] | None = None, cursor: str | None = None) -> Any:
        """Run a tool that only reads, or runs a check that changes nothing (from find_tool).

        A tool that can change something is refused with "not_a_read_tool".
        cursor: the next_cursor from a reply that was cut short, to get the rest.
        """
        return await dispatch.invoke_read_tool(name, arguments, cursor)

    async def invoke_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Run any tool from find_tool, including ones that change the network.

        The tool's own kind (from find_tool) says what it changes.
        """
        return await dispatch.invoke_tool(name, arguments)

    async def access_check() -> dict[str, Any]:
        """What each product login can do, and whether this server is read-only."""
        from casper_network_mcp.access import access_check as check

        return await check(server.clients, server.gate)

    server.add_tool(find_tool, annotations=READ_ONLY)
    server.add_tool(invoke_read_tool, annotations=READ_ONLY)
    server.add_tool(invoke_tool, annotations=WRITE)
    server.add_tool(access_check, annotations=READ_ONLY)
    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="casper-network-mcp",
        description="MCP server for HPE Aruba Central, Juniper Mist and HPE Aruba ClearPass.",
    )
    parser.add_argument(
        "--read-only", action="store_true", help="only read and run checks; never send a change (read once at start)"
    )
    parser.add_argument("--transport", choices=TRANSPORTS, default="stdio", help="stdio (default) or http")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP address on this machine (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help=f"HTTP port (default {DEFAULT_HTTP_PORT})")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    server = build_server(read_only=args.read_only)
    run_server(server, transport=args.transport, host=args.host, port=args.port)
