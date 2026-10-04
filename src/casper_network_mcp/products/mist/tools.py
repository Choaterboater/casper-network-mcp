"""Mist curated tools, and the ``mist`` backend that holds every hand-written Mist tool.

Adapted from hpe-networking-mcp ``mcp_servers/mist.py`` (MIT,
nowireless4u/hpe-networking-mcp). Endpoints and field names were checked
against the bundled Mist OpenAPI document. What changed:

* every request goes through the gated Mist client (``MistClient.request``):
  the read-only pin first, then the path check, then the login;
* every id in a path goes through ``path_segment`` (a ``/``, ``?``, ``#``,
  ``%`` or ``..`` is refused before anything is sent);
* no ``confirm`` argument and no server-side write switch: approval is
  Casper's box. Change tools take ``dry_run: bool = False``; with
  ``dry_run=True`` they return ``{"would_send": {method, path, body}}`` and
  send nothing;
* the generic ``mist_write`` passthrough is gone (each endpoint has its own
  generated tool with an honest change kind); ``mist_get`` stays, for reads only;
* ``mist_status`` is gone (``access_check`` reports the login); the browser
  session-cookie login and the WebSocket result collector are not ported
  (this package has no WebSocket dependency);
* ``mist_update_wlan`` is new (the plan's preview test), on the spec's
  ``PUT /sites/{site_id}/wlans/{wlan_id}``.

The other modules (``sle``, ``alarms``, ``events``, ``inventory``,
``marvis``) carry mist-mcp's tools; :func:`backend` registers them all.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.budget import bound_collection_response, clamp_limit
from casper_network_mcp.core.kinds import kind_for_operation
from casper_network_mcp.core.paths import safe_api_path
from casper_network_mcp.products._tools import ClientGetter, use_client
from casper_network_mcp.products.mist._common import (
    MIST,
    bool_param,
    change,
    extract_items,
    get,
    mist_client,
    normalize_mac,
    pick,
    seg,
)

__all__ = ["backend", "backend_tools"]

mcp = MIST

SleScope = Literal["ap", "client", "gateway", "site", "switch"]

_SITE_FIELDS = ("id", "name", "timezone", "country_code", "address", "latlng", "sitegroup_ids", "wifi_enabled")
_CLIENT_FIELDS = (
    "mac", "hostname", "ip", "username", "ap", "ap_id", "ap_name", "site_id", "ssid", "wlan_id", "vlan",
    "rssi", "snr", "band", "channel", "tx_rate", "rx_rate", "tx_bps", "rx_bps", "uptime", "last_seen",
    "health", "score", "connected", "assoc_time", "device", "os", "model",
)  # fmt: skip
_WLAN_FIELDS = ("id", "name", "ssid", "enabled", "auth", "auth_servers", "vlan_id", "wlan_id", "template_id", "site_id")
_ALARM_FIELDS = (
    "id", "type", "group", "severity", "timestamp", "last_seen", "count", "acked", "text", "reason",
    "device", "device_name", "ap", "client", "site_id",
)  # fmt: skip
# Spec ``nac_tag``: the VLAN field is ``vlan`` (string), not ``vlan_id``.
_NAC_TAG_FIELDS = ("id", "name", "type", "match", "match_all", "values", "vlan", "org_id")
# Spec ``nac_portal``: the read-only URLs are portal_sso_url, portal_authorize_url and ui_url.
_NAC_PORTAL_FIELDS = ("id", "name", "type", "ssid", "portal_sso_url", "portal_authorize_url", "ui_url", "org_id")
# Spec ``org_setting_mist_nac_idp``: a realm mapping to an identity provider id.
_NAC_IDP_FIELDS = ("id", "user_realms", "exclude_realms")
_USER_MAC_FIELDS = ("id", "mac", "name", "labels", "vlan", "radius_group", "notes")
# Spec ``inventory``: ``magic`` (the single-use claim code) is left out on purpose.
_INVENTORY_FIELDS = (
    "id", "mac", "serial", "model", "type", "sku", "hw_rev", "name", "hostname", "site_id", "org_id",
    "adopted", "connected", "last_disconnected", "vc_mac",
)  # fmt: skip
_SWITCH_FIELDS = ("id", "mac", "name", "model", "serial", "version", "status", "ip", "uptime", "site_id", "last_seen")
_PORT_FIELDS = (
    "port_id", "up", "full_duplex", "speed", "poe_disabled", "poe_mode", "poe_on", "mac", "neighbor_mac",
    "neighbor_port_desc", "neighbor_system_name", "stp_state", "stp_role",
)  # fmt: skip
_GATEWAY_FIELDS = (
    "id", "mac", "name", "model", "serial", "version", "status", "ip", "uptime", "is_ha", "cluster_config",
    "tunnels", "vpn_peers", "site_id", "last_seen",
)  # fmt: skip
_MARVIS_CLIENT_FIELDS = (
    "device_id", "hostname", "model", "mfg", "serial", "os_type", "os_version", "wifi_mac", "wifi_ip",
    "wifi_ssid", "wifi_rssi", "timestamp",
)  # fmt: skip
_EVENT_FIELDS = (
    "type", "ev_type", "timestamp", "site_id", "site_name", "mac", "model", "device_name", "device_type",
    "ap", "ap_name", "port_id", "text", "reason",
)  # fmt: skip


def _bounded(items: list[Any], fields: tuple[str, ...], limit: int, offset: int = 0) -> Any:
    return bound_collection_response([pick(item, fields) for item in items], limit=limit, offset=offset)


# ── read passthrough ────────────────────────────────────────────────────────


@mcp.tool()
async def mist_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """Read any Mist API path with GET (for endpoints no other tool covers).

    Only ``/api/v1/...`` paths, and only GETs that change nothing (a GET the
    spec marks as starting work, such as installer RF optimisation, is
    refused). Lists are bounded with ``limit`` and ``offset``.
    """
    safe = safe_api_path(path, ("/api/v1/",))
    if kind_for_operation("GET", safe) != "read":
        return {"error": f"Not sent: GET {safe} changes something in Mist, so mist_get does not run it."}
    data = await mist_client().request("GET", path, params=params or None, kind="read")
    return bound_collection_response(data, limit=clamp_limit(limit), offset=max(0, offset))


# ── sites, clients, WLANs, alarms ───────────────────────────────────────────


@mcp.tool()
async def mist_list_sites(org_id: str, limit: int = 100, page: int = 1) -> dict[str, Any]:
    """List the org's Mist sites (id, name, timezone, address).

    Uses ``GET /api/v1/orgs/{org_id}/sites``. Mist pages by ``page``; pass
    ``limit`` and ``page`` to move through a large org.
    """
    safe_limit = clamp_limit(limit, default=100)
    data = await get(f"/api/v1/orgs/{seg(org_id)}/sites", {"limit": safe_limit, "page": max(1, page)}, org_id=org_id)
    sites = _bounded(extract_items(data), _SITE_FIELDS, safe_limit)
    if isinstance(sites, dict):
        sites["server_page"] = max(1, page)
    return {"sites": sites}


@mcp.tool()
async def mist_get_client(site_id: str, mac_address: str) -> dict[str, Any]:
    """Look up one wireless client's health by site and MAC address.

    Uses ``GET /api/v1/sites/{site_id}/stats/clients/{client_mac}``: AP,
    WLAN, RSSI, SNR and identity fields.
    """
    normalized = normalize_mac(mac_address)
    data = await get(f"/api/v1/sites/{seg(site_id)}/stats/clients/{normalized}", site_id=site_id)
    return {"normalized_mac": normalized, "client": pick(data, _CLIENT_FIELDS)}


@mcp.tool()
async def mist_list_wlans(site_id: str, limit: int = 100, page: int = 1) -> dict[str, Any]:
    """List a site's WLANs (SSID, on/off, security, VLAN). Passphrases are hidden.

    Uses ``GET /api/v1/sites/{site_id}/wlans``; Mist pages by ``page``.
    """
    safe_limit = clamp_limit(limit, default=100)
    data = await get(
        f"/api/v1/sites/{seg(site_id)}/wlans", {"limit": safe_limit, "page": max(1, page)}, site_id=site_id
    )
    wlans = _bounded(extract_items(data), _WLAN_FIELDS, safe_limit)
    if isinstance(wlans, dict):
        wlans["server_page"] = max(1, page)
    return {"wlans": wlans}


@mcp.tool()
async def mist_update_wlan(site_id: str, wlan_id: str, changes: dict[str, Any], dry_run: bool = False) -> Any:
    """Change one site WLAN: only the fields given in ``changes`` (for example ``{"vlan_id": 30}``).

    Uses ``PUT /api/v1/sites/{site_id}/wlans/{wlan_id}``. With
    ``dry_run=True`` it returns the request it would send and sends nothing.
    """
    if not changes:
        raise ValueError("changes must name at least one WLAN field to change")
    path = f"/api/v1/sites/{seg(site_id)}/wlans/{seg(wlan_id)}"
    out = await change("PUT", path, body=changes, dry_run=dry_run, site_id=site_id)
    return out if dry_run else {"wlan": out}


@mcp.tool()
async def mist_delete_wlan(site_id: str, wlan_id: str, dry_run: bool = False) -> Any:
    """Delete one site WLAN (``DELETE /api/v1/sites/{site_id}/wlans/{wlan_id}``).

    With ``dry_run=True`` it returns the request it would send and sends nothing.
    """
    path = f"/api/v1/sites/{seg(site_id)}/wlans/{seg(wlan_id)}"
    out = await change("DELETE", path, dry_run=dry_run, site_id=site_id)
    return out if dry_run else {"deleted": True, "wlan_id": wlan_id}


@mcp.tool()
async def mist_list_alarms(
    site_id: str,
    severity: Literal["critical", "info", "warn"] | None = None,
    duration: str = "1d",
    limit: int = 100,
    start: str | None = None,
    end: str | None = None,
    search_after: str | None = None,
) -> dict[str, Any]:
    """List a site's recent alarms, newest first.

    Uses ``GET /api/v1/sites/{site_id}/alarms/search``. Pass ``search_after``
    from a previous reply to continue.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {
        "severity": severity,
        "limit": safe_limit,
        "start": start,
        "end": end,
        "duration": duration,
        "sort": "-timestamp",
        "search_after": search_after,
    }
    data = await get(f"/api/v1/sites/{seg(site_id)}/alarms/search", params, site_id=site_id)
    out: dict[str, Any] = {"alarms": _bounded(extract_items(data), _ALARM_FIELDS, safe_limit)}
    if isinstance(data, dict) and data.get("next"):
        out["next"] = data["next"]
    return out


@mcp.tool()
async def mist_ack_alarm(site_id: str, alarm_id: str, note: str | None = None, dry_run: bool = False) -> Any:
    """Acknowledge one site alarm (``POST /api/v1/sites/{site_id}/alarms/{alarm_id}/ack``)."""
    path = f"/api/v1/sites/{seg(site_id)}/alarms/{seg(alarm_id)}/ack"
    return await change("POST", path, body={"note": note} if note else None, dry_run=dry_run, site_id=site_id)


@mcp.tool()
async def mist_unack_alarm(site_id: str, alarm_id: str, note: str | None = None, dry_run: bool = False) -> Any:
    """Reopen (un-acknowledge) one site alarm (``POST .../alarms/{alarm_id}/unack``)."""
    path = f"/api/v1/sites/{seg(site_id)}/alarms/{seg(alarm_id)}/unack"
    return await change("POST", path, body={"note": note} if note else None, dry_run=dry_run, site_id=site_id)


# ── NAC / Access Assurance ──────────────────────────────────────────────────


@mcp.tool()
async def mist_list_nac_tags(org_id: str, limit: int = 100, page: int = 1) -> dict[str, Any]:
    """List the org's NAC tags used by Access Assurance rules (``GET /api/v1/orgs/{org_id}/nactags``)."""
    safe_limit = clamp_limit(limit, default=100)
    data = await get(f"/api/v1/orgs/{seg(org_id)}/nactags", {"limit": safe_limit, "page": max(1, page)}, org_id=org_id)
    return {"nac_tags": _bounded(extract_items(data), _NAC_TAG_FIELDS, safe_limit)}


@mcp.tool()
async def mist_list_nac_portals(org_id: str, limit: int = 100, page: int = 1) -> dict[str, Any]:
    """List the org's NAC (guest/BYOD) portals (``GET /api/v1/orgs/{org_id}/nacportals``)."""
    safe_limit = clamp_limit(limit, default=100)
    data = await get(
        f"/api/v1/orgs/{seg(org_id)}/nacportals", {"limit": safe_limit, "page": max(1, page)}, org_id=org_id
    )
    return {"nac_portals": _bounded(extract_items(data), _NAC_PORTAL_FIELDS, safe_limit)}


@mcp.tool()
async def mist_list_nac_idps(org_id: str) -> dict[str, Any]:
    """List the identity-provider realm mappings behind Access Assurance.

    The spec has no ``/nacidps`` resource: the mappings live in the org
    settings (``GET /api/v1/orgs/{org_id}/setting``, ``mist_nac.idps``).
    """
    data = await get(f"/api/v1/orgs/{seg(org_id)}/setting", org_id=org_id)
    mist_nac = data.get("mist_nac") if isinstance(data, dict) else None
    idps = mist_nac.get("idps") if isinstance(mist_nac, dict) else None
    return {"nac_idps": _bounded(list(idps or []), _NAC_IDP_FIELDS, 100)}


@mcp.tool()
async def mist_list_user_macs(org_id: str, limit: int = 100, page: int = 1) -> dict[str, Any]:
    """List the org's known-client MAC entries (MAC to label/VLAN), often used by NAC rules.

    Uses ``GET /api/v1/orgs/{org_id}/usermacs/search`` (the spec has no GET
    on ``/usermacs`` itself).
    """
    safe_limit = clamp_limit(limit, default=100)
    data = await get(
        f"/api/v1/orgs/{seg(org_id)}/usermacs/search", {"limit": safe_limit, "page": max(1, page)}, org_id=org_id
    )
    return {"user_macs": _bounded(extract_items(data), _USER_MAC_FIELDS, safe_limit)}


@mcp.tool()
async def mist_upsert_user_mac(
    org_id: str,
    mac_address: str,
    labels: list[str] | None = None,
    vlan: str | None = None,
    radius_group: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
) -> Any:
    """Add one known-client MAC entry for NAC rules (``POST /api/v1/orgs/{org_id}/usermacs``).

    ``vlan`` is text in Mist's ``user_mac`` schema. With ``dry_run=True`` it
    returns the request it would send and sends nothing.
    """
    normalized = normalize_mac(mac_address)
    body: dict[str, Any] = {"mac": normalized}
    for key, value in (("labels", labels), ("vlan", vlan), ("radius_group", radius_group), ("notes", notes)):
        if value is not None:
            body[key] = value
    out = await change("POST", f"/api/v1/orgs/{seg(org_id)}/usermacs", body=body, dry_run=dry_run, org_id=org_id)
    if isinstance(out, dict):
        out = {**out, "normalized_mac": normalized}
    return out


# ── Marvis ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def mist_search_marvis_clients(
    org_id: str,
    hostname: str | None = None,
    model: str | None = None,
    serial: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search Marvis Client app telemetry (phones and laptops running the app) in one org.

    Uses ``GET /api/v1/orgs/{org_id}/stats/marvisclients/search``.
    """
    safe_limit = clamp_limit(limit, default=50)
    params = {"hostname": hostname, "model": model, "serial": serial, "limit": safe_limit}
    data = await get(f"/api/v1/orgs/{seg(org_id)}/stats/marvisclients/search", params, org_id=org_id)
    return {"marvis_clients": _bounded(extract_items(data), _MARVIS_CLIENT_FIELDS, safe_limit, max(0, offset))}


@mcp.tool()
async def mist_get_client_insights(
    site_id: str,
    client_mac: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Experience metrics over time for one wireless client.

    Uses ``GET /api/v1/sites/{site_id}/insights/client/{client_mac}``;
    ``start``/``end`` are epoch seconds.
    """
    normalized = normalize_mac(client_mac)
    data = await get(
        f"/api/v1/sites/{seg(site_id)}/insights/client/{normalized}", {"start": start, "end": end}, site_id=site_id
    )
    return {"normalized_mac": normalized, "insights": data}


@mcp.tool()
async def mist_search_events(
    site_id: str,
    event_type: str | None = None,
    mac: str | None = None,
    model: str | None = None,
    text: str | None = None,
    duration: str = "1d",
    limit: int = 100,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Search a site's device events (APs, switches, gateways), newest first.

    Uses ``GET /api/v1/sites/{site_id}/devices/events/search``. ``event_type``
    is Mist's ``type`` filter. For system or client events use ``site_events``.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {
        "type": event_type,
        "mac": mac,
        "model": model,
        "text": text,
        "limit": safe_limit,
        "start": start,
        "end": end,
        "duration": duration,
        "sort": "-timestamp",
    }
    data = await get(f"/api/v1/sites/{seg(site_id)}/devices/events/search", params, site_id=site_id)
    return {"events": _bounded(extract_items(data), _EVENT_FIELDS, safe_limit)}


@mcp.tool()
async def mist_get_marvis_settings(org_id: str) -> dict[str, Any]:
    """The org's Marvis settings (the ``marvis`` part of ``GET /api/v1/orgs/{org_id}/setting``)."""
    data = await get(f"/api/v1/orgs/{seg(org_id)}/setting", org_id=org_id)
    return {"marvis_settings": (data.get("marvis") if isinstance(data, dict) else None) or {}}


@mcp.tool()
async def mist_set_marvis_settings(org_id: str, settings: dict[str, Any], dry_run: bool = False) -> Any:
    """Change the org's Marvis settings.

    Uses ``PUT /api/v1/orgs/{org_id}/setting`` with ``settings`` under the
    ``marvis`` key (for example ``{"disable_proactive_monitoring": true}``).
    With ``dry_run=True`` it returns the request it would send and sends nothing.
    """
    path = f"/api/v1/orgs/{seg(org_id)}/setting"
    return await change("PUT", path, body={"marvis": settings}, dry_run=dry_run, org_id=org_id)


# ── inventory ───────────────────────────────────────────────────────────────


@mcp.tool()
async def mist_list_org_inventory(
    org_id: str,
    device_type: Literal["ap", "switch", "gateway"] | None = None,
    unassigned: bool | None = None,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List the org's device inventory (claimed devices, assigned to a site or not).

    Uses ``GET /api/v1/orgs/{org_id}/inventory``. Claim codes are left out.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {"type": device_type, "unassigned": bool_param(unassigned), "limit": safe_limit, "page": max(1, page)}
    data = await get(f"/api/v1/orgs/{seg(org_id)}/inventory", params, org_id=org_id)
    return {"inventory": _bounded(extract_items(data), _INVENTORY_FIELDS, safe_limit)}


@mcp.tool()
async def mist_claim_devices(org_id: str, claim_codes: list[str], dry_run: bool = False) -> Any:
    """Claim devices into the org's inventory by claim code.

    Uses ``POST /api/v1/orgs/{org_id}/inventory`` with the codes as a plain
    list. Codes are single-use secrets, so the preview shows only their last
    four characters.
    """
    if not claim_codes:
        raise ValueError("claim_codes must contain at least one claim code.")
    masked = [f"...{code[-4:]}" if len(code) > 4 else "****" for code in claim_codes]
    path = f"/api/v1/orgs/{seg(org_id)}/inventory"
    return await change("POST", path, body=list(claim_codes), preview_body=masked, dry_run=dry_run, org_id=org_id)


# ── Wired and WAN Assurance ─────────────────────────────────────────────────

_DeviceStatus = Literal["all", "connected", "disconnected"]


async def _device_stats(site_id: str, device_type: str, status: str, limit: int, page: int) -> Any:
    safe_limit = clamp_limit(limit, default=100)
    params = {"type": device_type, "status": status, "limit": safe_limit, "page": max(1, page)}
    return await get(f"/api/v1/sites/{seg(site_id)}/stats/devices", params, site_id=site_id), safe_limit


@mcp.tool()
async def mist_list_switches(
    site_id: str, status: _DeviceStatus = "all", limit: int = 100, page: int = 1
) -> dict[str, Any]:
    """List a site's switches with status, version and uptime.

    Uses ``GET /api/v1/sites/{site_id}/stats/devices?type=switch`` (Mist has
    one device-stats resource for every device type).
    """
    data, safe_limit = await _device_stats(site_id, "switch", status, limit, page)
    return {"switches": _bounded(extract_items(data), _SWITCH_FIELDS, safe_limit)}


@mcp.tool()
async def mist_list_switch_ports(site_id: str, switch_mac: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Port status for one switch: link, speed, PoE, STP and LLDP neighbour.

    Uses ``GET /api/v1/sites/{site_id}/stats/ports/search`` filtered by the
    switch MAC (the spec pages this search with ``search_after``, not pages).
    """
    normalized = normalize_mac(switch_mac)
    params = {"mac": normalized, "device_type": "switch", "limit": clamp_limit(limit, default=100)}
    data = await get(f"/api/v1/sites/{seg(site_id)}/stats/ports/search", params, site_id=site_id)
    return {
        "normalized_mac": normalized,
        "ports": _bounded(extract_items(data), _PORT_FIELDS, clamp_limit(limit, default=100), max(0, offset)),
    }


@mcp.tool()
async def mist_list_gateways(
    site_id: str, status: _DeviceStatus = "all", limit: int = 100, page: int = 1
) -> dict[str, Any]:
    """List a site's WAN Edge gateways (SRX or SSR; ``model`` tells them apart), with HA and tunnels.

    Uses ``GET /api/v1/sites/{site_id}/stats/devices?type=gateway``.
    """
    data, safe_limit = await _device_stats(site_id, "gateway", status, limit, page)
    return {"gateways": _bounded(extract_items(data), _GATEWAY_FIELDS, safe_limit)}


@mcp.tool()
async def mist_get_gateway(site_id: str, device_id: str) -> dict[str, Any]:
    """One WAN Edge gateway's status (``GET /api/v1/sites/{site_id}/stats/devices/{device_id}``)."""
    data = await get(f"/api/v1/sites/{seg(site_id)}/stats/devices/{seg(device_id)}", site_id=site_id)
    return {"gateway": pick(data, _GATEWAY_FIELDS)}


async def _section(coro: Any) -> Any:
    from casper_network_mcp.products._tools import _HANDLED, _as_error

    try:
        return await coro
    except _HANDLED as exc:
        return _as_error("mist", exc)


@mcp.tool()
async def mist_get_site_assurance_snapshot(
    site_id: str,
    include_switches: bool = True,
    include_gateways: bool = True,
    include_alarms: bool = True,
    alarm_duration: str = "1d",
    limit: int = 50,
) -> dict[str, Any]:
    """One-call site health: switches, gateways and recent alarms together.

    Runs ``mist_list_switches``, ``mist_list_gateways`` and
    ``mist_list_alarms`` at once. Each section stands alone: a failed section
    carries its own ``error`` and ``degraded`` is true.
    """
    seg(site_id)
    safe_limit = clamp_limit(limit, default=50)
    calls: dict[str, Any] = {}
    if include_switches:
        calls["switches"] = mist_list_switches(site_id, limit=safe_limit)
    if include_gateways:
        calls["gateways"] = mist_list_gateways(site_id, limit=safe_limit)
    if include_alarms:
        calls["alarms"] = mist_list_alarms(site_id, duration=alarm_duration, limit=safe_limit)
    if not calls:
        return {"error": "At least one of include_switches, include_gateways or include_alarms must be true."}
    results = await asyncio.gather(*(_section(c) for c in calls.values()))
    sections = dict(zip(calls, results, strict=True))
    return {
        "site_id": site_id,
        "sections": sections,
        "degraded": any(isinstance(r, dict) and "error" in r for r in results),
    }


# ── SLE convenience reads ───────────────────────────────────────────────────


@mcp.tool()
async def mist_get_org_sle_overview(
    org_id: str,
    metric: str = "throughput",
    start: str | None = None,
    end: str | None = None,
    duration: str = "1d",
) -> dict[str, Any]:
    """An org-wide SLE insight metric (``GET /api/v1/orgs/{org_id}/insights/{metric}``).

    Common ``metric`` values: throughput, wifi-success-connecting,
    time-to-connect, roam-success, coverage. ``start``/``end`` are epoch
    seconds, or use ``duration`` (``1d``, ``7d``).
    """
    data = await get(
        f"/api/v1/orgs/{seg(org_id)}/insights/{seg(metric)}",
        {"duration": duration, "start": start, "end": end},
        org_id=org_id,
    )
    return {"org_id": org_id, "metric": metric, "sle": bound_collection_response(data, limit=clamp_limit(None))}


@mcp.tool()
async def mist_get_site_sle_metric_summary(
    site_id: str,
    scope: SleScope,
    scope_id: str,
    metric: str = "wifi",
    start: str | None = None,
    end: str | None = None,
    duration: str = "1d",
) -> dict[str, Any]:
    """One SLE metric's summary for a scope at a site.

    Uses ``GET /api/v1/sites/{site_id}/sle/{scope}/{scope_id}/metric/{metric}/summary``.
    ``scope`` is ap, client, gateway, site or switch (the spec's scopes);
    ``scope_id`` is the device id, the client MAC, or the site id when scope
    is site. ``start``/``end`` are epoch seconds, or use ``duration``.
    """
    path = f"/api/v1/sites/{seg(site_id)}/sle/{seg(scope)}/{seg(scope_id)}/metric/{seg(metric)}/summary"
    data = await get(path, {"duration": duration, "start": start, "end": end}, site_id=site_id)
    return {
        "site_id": site_id,
        "scope": scope,
        "scope_id": scope_id,
        "metric": metric,
        "summary": bound_collection_response(data, limit=clamp_limit(None)),
    }


# ── the backend ─────────────────────────────────────────────────────────────


def backend(client: ClientGetter | None = None) -> MCPServer:
    """The ``mist`` backend: every hand-written Mist tool, labelled from ``labels.yaml``.

    ``client`` is the getter the tools call at call time (the server passes
    one with its gate; tests pass one on a recording fake).
    """
    from casper_network_mcp.products.mist import alarms, events, inventory, marvis, sle  # noqa: F401

    if client is not None:
        use_client("mist", client)
    server = MCPServer("mist")
    MIST.register(server)
    return server


def backend_tools() -> list[Any]:
    """The registered Mist tools (``.name``, ``.fn``, ``.parameters``)."""
    return list(sdk_compat.tool_registry(backend()).values())
