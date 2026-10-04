"""Event, audit-log and rogue-AP searches.

Adapted from mist-mcp ``mist_mcp/tools/events.py`` (MIT); ``insight_metric`` is
not ported. Changed: ``org_id``/``site_id`` are required arguments, and a MAC
filter is accepted only for device events, the only event search whose spec
has a ``mac`` parameter (client and system event searches have none).
"""

from __future__ import annotations

from typing import Any, Literal

from casper_network_mcp.products.mist._common import MIST, get, seg

mcp = MIST

Kind = Literal["system", "devices", "clients"]

_SITE_PATHS = {
    "system": "/api/v1/sites/{id}/events/system/search",
    "devices": "/api/v1/sites/{id}/devices/events/search",
    "clients": "/api/v1/sites/{id}/clients/events/search",
}
_ORG_PATHS = {
    "system": "/api/v1/orgs/{id}/events/search",
    "devices": "/api/v1/orgs/{id}/devices/events/search",
    "clients": "/api/v1/orgs/{id}/clients/events/search",
}


def _search_params(
    kind: str,
    event_type: str | None,
    mac: str | None,
    duration: str | None,
    start: int | None,
    end: int | None,
    limit: int,
    search_after: str | None,
) -> dict[str, Any]:
    if mac and kind != "devices":
        raise ValueError(
            "mac narrows device events only (kind=devices); Mist's other event searches have no mac filter"
        )
    params: dict[str, Any] = {"limit": limit}
    if not (start or end):
        params["duration"] = duration or "1d"
    params.update({"type": event_type, "start": start, "end": end, "search_after": search_after, "mac": mac})
    return params


@mcp.tool()
async def site_events(
    site_id: str,
    kind: Kind = "system",
    event_type: str | None = None,
    mac: str | None = None,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 100,
    search_after: str | None = None,
) -> Any:
    """Search a site's events.

    ``kind``: system (site and config events, default), devices (AP, switch,
    gateway events) or clients (connect, roam, auth). ``mac`` narrows device
    events to one device. ``duration``: "1h", "1d" (default), "7d"; or
    ``start``/``end`` epoch seconds. Page on with ``search_after``.
    """
    params = _search_params(kind, event_type, mac, duration, start, end, limit, search_after)
    return await get(_SITE_PATHS[kind].format(id=seg(site_id)), params, site_id=site_id)


@mcp.tool()
async def org_events(
    org_id: str,
    kind: Kind = "system",
    event_type: str | None = None,
    mac: str | None = None,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 100,
    search_after: str | None = None,
) -> Any:
    """Search the org's events. ``kind``: system (default), devices or clients.

    ``mac`` narrows device events. ``duration``: "1d" (default), "7d"; or
    ``start``/``end`` epoch seconds. Page on with ``search_after``.
    """
    params = _search_params(kind, event_type, mac, duration, start, end, limit, search_after)
    return await get(_ORG_PATHS[kind].format(id=seg(org_id)), params, org_id=org_id)


@mcp.tool()
async def audit_logs(
    org_id: str,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
    admin_name: str | None = None,
    message: str | None = None,
    site_id: str | None = None,
    limit: int = 100,
    page: int | None = None,
) -> Any:
    """Who changed what and when: the org's admin audit log (``GET /api/v1/orgs/{org_id}/logs``).

    ``admin_name`` and ``message`` filter; ``duration`` "1d" (default), "7d";
    or ``start``/``end`` epoch seconds.
    """
    params: dict[str, Any] = {"limit": limit}
    if not (start or end):
        params["duration"] = duration or "1d"
    params.update(
        {"start": start, "end": end, "admin_name": admin_name, "message": message, "site_id": site_id, "page": page}
    )
    return await get(f"/api/v1/orgs/{seg(org_id)}/logs", params, org_id=org_id)


@mcp.tool()
async def rogue_aps(
    site_id: str,
    rogue_type: Literal["honeypot", "lan", "others", "spoof"] | None = None,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 100,
) -> Any:
    """Rogue and neighbour APs a site's APs heard (``GET /api/v1/sites/{site_id}/insights/rogues``).

    ``rogue_type``: honeypot, lan, spoof or others. ``duration`` "1d"
    (default); or ``start``/``end`` epoch seconds.
    """
    params: dict[str, Any] = {"limit": limit}
    if not (start or end):
        params["duration"] = duration or "1d"
    params.update({"type": rogue_type, "start": start, "end": end})
    return await get(f"/api/v1/sites/{seg(site_id)}/insights/rogues", params, site_id=site_id)
