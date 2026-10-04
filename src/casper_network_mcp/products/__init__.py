"""Product backends: Central, Mist and ClearPass, plus the bundled-spec lookup."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

__all__ = ["hand_written_backends"]


def hand_written_backends() -> list[tuple[str, MCPServer]]:
    """``(product, backend)`` for every hand-written product backend.

    Tasks 7-9 add Mist, Central and ClearPass here as their tools are ported;
    each tool's label and change kind come from its product's ``labels.yaml``.
    """
    from casper_network_mcp.products.central.tools import backends as central_backends
    from casper_network_mcp.products.mist.tools import backend as mist_backend

    return [("mist", mist_backend()), *(("central", server) for _, server in central_backends())]
