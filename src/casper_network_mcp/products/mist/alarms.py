"""Org alarm search and the ack/unack lifecycle.

Adapted from mist-mcp ``mist_mcp/tools/alarms.py`` (MIT). Changed: ``org_id``
is a required argument, the lifecycle tools take ``dry_run`` (opt-in preview)
and there is no read-only env switch (the gate decides). The alarm template
tools are not ported (each has a generated tool).
"""

from __future__ import annotations

from typing import Any, Literal

from casper_network_mcp.products.mist._common import MIST, bool_param, change, get, seg

mcp = MIST


@mcp.tool()
async def alarms_list(
    org_id: str,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
    acked: bool | None = None,
    severity: str | None = None,
    alarm_type: str | None = None,
    group: Literal["certificate_expiry", "infrastructure", "marvis", "security"] | None = None,
    site_id: str | None = None,
    limit: int = 100,
    search_after: str | None = None,
) -> Any:
    """Search the org's alarms (``GET /api/v1/orgs/{org_id}/alarms/search``).

    ``duration``: "1h", "1d" (default), "7d"; or ``start``/``end`` epoch
    seconds. ``acked`` true/false; ``severity``; ``alarm_type`` (Mist's
    alarm type); ``group``; ``site_id`` for one site. The reply's ``next``
    link (or ``search_after``) fetches the following page.
    """
    params: dict[str, Any] = {"limit": limit}
    if not (start or end):
        params["duration"] = duration or "1d"
    params.update(
        {
            "start": start,
            "end": end,
            "acked": bool_param(acked),
            "severity": severity,
            "type": alarm_type,
            "group": group,
            "site_id": site_id,
            "search_after": search_after,
        }
    )
    return await get(f"/api/v1/orgs/{seg(org_id)}/alarms/search", params, org_id=org_id)


async def _lifecycle(action: str, org_id: str, alarm_ids: list[str], note: str | None, dry_run: bool) -> Any:
    if not alarm_ids:
        raise ValueError("provide alarm_ids")
    body: dict[str, Any] = {"alarm_ids": list(alarm_ids)}
    if note:
        body["note"] = note
    return await change(
        "POST", f"/api/v1/orgs/{seg(org_id)}/alarms/{action}", body=body, dry_run=dry_run, org_id=org_id
    )


@mcp.tool()
async def alarms_ack(org_id: str, alarm_ids: list[str], note: str | None = None, dry_run: bool = False) -> Any:
    """Acknowledge alarms (``POST /api/v1/orgs/{org_id}/alarms/ack``), with an optional note."""
    return await _lifecycle("ack", org_id, alarm_ids, note, dry_run)


@mcp.tool()
async def alarms_unack(org_id: str, alarm_ids: list[str], note: str | None = None, dry_run: bool = False) -> Any:
    """Reopen (un-acknowledge) alarms (``POST /api/v1/orgs/{org_id}/alarms/unack``)."""
    return await _lifecycle("unack", org_id, alarm_ids, note, dry_run)
