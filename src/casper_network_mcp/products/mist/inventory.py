"""Inventory changes: assign to a site, unassign, or release from the org.

Adapted from mist-mcp ``mist_mcp/tools/inventory.py`` (MIT). The source's one
``inventory_update(op=...)`` tool is split into three, so each has an honest
change kind: assign and unassign are ``config``, release (``op=delete``) is
``delete``. All use ``PUT /api/v1/orgs/{org_id}/inventory``. Claiming is
``mist_claim_devices`` (in ``tools.py``).
"""

from __future__ import annotations

from typing import Any

from casper_network_mcp.products.mist._common import MIST, change, normalize_mac, seg

mcp = MIST


async def _update(op: str, org_id: str, macs: list[str], extra: dict[str, Any], dry_run: bool) -> Any:
    if not macs:
        raise ValueError("macs must contain at least one device MAC")
    body: dict[str, Any] = {"op": op, "macs": [normalize_mac(m) for m in macs], **extra}
    return await change("PUT", f"/api/v1/orgs/{seg(org_id)}/inventory", body=body, dry_run=dry_run, org_id=org_id)


@mcp.tool()
async def inventory_assign(
    org_id: str, macs: list[str], site_id: str, no_reassign: bool | None = None, dry_run: bool = False
) -> Any:
    """Assign inventory devices to a site.

    ``no_reassign``: leave devices already on another site alone. With
    ``dry_run=True`` it returns the request it would send and sends nothing.
    """
    extra: dict[str, Any] = {"site_id": site_id}
    if no_reassign is not None:
        extra["no_reassign"] = no_reassign
    return await _update("assign", org_id, macs, extra, dry_run)


@mcp.tool()
async def inventory_unassign(org_id: str, macs: list[str], dry_run: bool = False) -> Any:
    """Unassign inventory devices from their site (they stay in the org)."""
    return await _update("unassign", org_id, macs, {}, dry_run)


@mcp.tool()
async def inventory_delete(org_id: str, macs: list[str], dry_run: bool = False) -> Any:
    """Release devices from the org's inventory (``op=delete``); they can be claimed again later."""
    return await _update("delete", org_id, macs, {}, dry_run)
