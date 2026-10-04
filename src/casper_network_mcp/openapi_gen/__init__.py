"""Generated tools: one MCP tool per operation in the bundled vendor specs.

Adapted from hpe-networking-mcp's ``openapi_gen`` package (MIT,
nowireless4u/hpe-networking-mcp):

* :mod:`.ir` -- Swagger 2.0 / OpenAPI 3.0 / 3.1 parsing into a flat list of operations.
* :mod:`.naming` -- deterministic, unique tool names.
* :mod:`.manifest` -- build and load the committed per-product manifest.
* :mod:`.runtime` -- register manifest operations as tools.
* :mod:`.http_exec` -- send a generated call through the product client's gated ``request()``.
"""

from __future__ import annotations

from casper_network_mcp.openapi_gen.manifest import Manifest, load_manifest

__all__ = ["Manifest", "load_manifest"]
