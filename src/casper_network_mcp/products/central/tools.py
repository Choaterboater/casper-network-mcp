"""The four hand-written Central backends: monitoring, config, ops and NAC.

Each module (copied from hpe-networking-mcp, see their notes) collects its
tools in a ``ToolSet``; this builds one backend per module, with every tool's
label and change kind taken from ``labels.yaml``.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from casper_network_mcp.products._tools import ClientGetter, use_client

__all__ = ["backends"]


def backends(client: ClientGetter | None = None) -> list[tuple[str, MCPServer]]:
    """``[(name, backend)]`` for central-monitoring, central-config, central-ops and central-nac.

    ``client`` is the getter the tools call at call time (the server passes
    the gated Central client; tests pass one on a recording fake).
    """
    from casper_network_mcp.products.central import config, monitoring, nac, ops

    if client is not None:
        use_client("central", client)
    out: list[tuple[str, MCPServer]] = []
    for module in (monitoring, config, ops, nac):
        server = MCPServer(module.BACKEND_NAME)
        module.mcp.register(server)
        out.append((module.BACKEND_NAME, server))
    return out
