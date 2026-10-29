"""Product backends: Central, Mist, ClearPass, UXI and OpsRamp, plus the bundled-spec lookup."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

__all__ = ["hand_written_backends"]


def hand_written_backends() -> list[tuple[str, MCPServer]]:
    """``(product, backend)`` for every hand-written product backend.

    Mist (one backend), Central (monitoring, config, ops, NAC), ClearPass, UXI and
    OpsRamp (one backend each); each tool's label and change kind come from its product's
    ``labels.yaml``. The generated backends are separate (``openapi_gen``).
    """
    from casper_network_mcp.products.central.tools import backends as central_backends
    from casper_network_mcp.products.clearpass.tools import backend as clearpass_backend
    from casper_network_mcp.products.mist.tools import backend as mist_backend
    from casper_network_mcp.products.opsramp.tools import backend as opsramp_backend
    from casper_network_mcp.products.uxi.tools import backend as uxi_backend

    return [
        ("mist", mist_backend()),
        *(("central", server) for _, server in central_backends()),
        ("clearpass", clearpass_backend()),
        ("uxi", uxi_backend()),
        ("opsramp", opsramp_backend()),
    ]
