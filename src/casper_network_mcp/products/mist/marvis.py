"""Marvis troubleshooting, as Mist's API defines it.

Adapted from mist-mcp ``mist_mcp/tools/marvis.py`` (MIT).
``GET /api/v1/orgs/{org_id}/troubleshoot`` asks Marvis what is wrong with a
client, a device or a whole site, over at most the last 7 days. Needs a
Marvis subscription. ``org_id`` is now a required argument.
"""

from __future__ import annotations

from typing import Any, Literal

from casper_network_mcp.products.mist._common import MIST, get, normalize_mac, seg

mcp = MIST


@mcp.tool()
async def marvis_troubleshoot(
    org_id: str,
    mac: str | None = None,
    site_id: str | None = None,
    network: Literal["wireless", "wired", "wan"] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Any:
    """Ask Marvis what is wrong (``GET /api/v1/orgs/{org_id}/troubleshoot``).

    ``mac``: a client or device MAC, to troubleshoot that client or device.
    Or ``site_id`` with ``network`` = wireless, wired or wan, for a whole
    site. ``start``/``end``: epoch seconds or relative such as "-1d" (at most
    the last 7 days). Needs Marvis.
    """
    if not mac and not site_id:
        raise ValueError("give a client or device mac, or a site_id")
    params: dict[str, Any] = {
        "mac": normalize_mac(mac) if mac else None,
        "site_id": site_id,
        "type": network,
        "start": start,
        "end": end,
    }
    return await get(f"/api/v1/orgs/{seg(org_id)}/troubleshoot", params, org_id=org_id)
