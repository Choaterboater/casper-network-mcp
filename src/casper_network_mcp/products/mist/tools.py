"""Mist curated tools, and the ``mist`` backend that holds every hand-written Mist tool.

Adapted from hpe-networking-mcp ``mcp_servers/mist.py``. Endpoints and field names were checked
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


@mcp.tool()
async def mist_whoami() -> dict[str, Any]:
    """Who this Mist login is, and which orgs and sites it can reach.

    Uses ``GET /api/v1/self``. Call it first when another tool wants an
    ``org_id`` (or ``site_id``) you have not been given yet. Returns
    ``identity``, ``orgs`` (id, name, role) and ``sites`` (id, name, org_id).
    """
    data = await get("/api/v1/self")
    if not isinstance(data, dict):
        return {"identity": None, "orgs": [], "sites": []}
    orgs: list[dict[str, Any]] = []
    sites: list[dict[str, Any]] = []
    seen_org: set[str] = set()
    seen_site: set[str] = set()

    def add_org(org_id: Any, name: Any, role: Any) -> None:
        if isinstance(org_id, str) and org_id not in seen_org:
            seen_org.add(org_id)
            orgs.append({"id": org_id, "name": name, "role": role})

    def add_site(site_id: Any, name: Any, org_id: Any) -> None:
        if isinstance(site_id, str) and site_id not in seen_site:
            seen_site.add(site_id)
            sites.append({"id": site_id, "name": name, "org_id": org_id})

    for privilege in data.get("privileges") or []:
        if not isinstance(privilege, dict):
            continue
        role = privilege.get("role")
        for org in privilege.get("orgs") or []:
            if isinstance(org, dict):
                add_org(org.get("org_id"), org.get("name"), role)
        for site in privilege.get("sites") or []:
            if isinstance(site, dict):
                add_site(site.get("site_id"), site.get("name"), site.get("org_id"))
        if privilege.get("scope") == "org":
            add_org(privilege.get("org_id"), privilege.get("name"), role)
        elif privilege.get("scope") == "site":
            add_site(privilege.get("site_id"), privilege.get("name"), privilege.get("org_id"))
    return {"identity": data.get("email") or data.get("name"), "orgs": orgs, "sites": sites}


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
async def mist_upsert_nac_mac(
    org_id: str,
    mac_address: str,
    labels: list[str] | None = None,
    vlan: str | None = None,
    radius_group: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
) -> Any:
    """Add one known-client MAC entry for NAC rules (``POST /api/v1/orgs/{org_id}/usermacs``).

    Mist calls these "User MACs" (formerly ``mist_upsert_user_mac``); the
    entry only maps a MAC to labels or a VLAN, it is no user account.
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
    metrics: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Experience metrics over time for one wireless client.

    Uses ``GET /api/v1/sites/{site_id}/insights/client/{client_mac}``.
    ``metrics`` is a comma-separated list of Mist insight metric names (Mist
    lists them at ``/api/v1/const/insight_metrics``, readable with mist_get);
    ``start``/``end`` are epoch seconds.
    """
    normalized = normalize_mac(client_mac)
    data = await get(
        f"/api/v1/sites/{seg(site_id)}/insights/client/{normalized}",
        {"metrics": metrics, "start": start, "end": end},
        site_id=site_id,
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


_SITE_STATS_FIELDS = (
    "id", "name", "timezone", "country_code", "address", "num_ap", "num_ap_connected", "num_clients",
    "num_devices", "num_devices_connected", "num_switch", "num_switch_connected", "num_gateway",
    "num_gateway_connected",
)  # fmt: skip
_SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}


def _device_counts(devices: list[Any]) -> dict[str, Any]:
    by_type: dict[str, dict[str, int]] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        kind = str(device.get("type") or "unknown")
        status = str(device.get("status") or "unknown")
        by_type.setdefault(kind, {})
        by_type[kind][status] = by_type[kind].get(status, 0) + 1
    return {"total": sum(sum(v.values()) for v in by_type.values()), "by_type": by_type}


@mcp.tool()
async def mist_site_overview(site_id: str, top_alarms: int = 5, alarm_duration: str = "1d") -> dict[str, Any]:
    """How a site is doing, in one call: health counts, devices by type and status, and the top alarms.

    Reads ``GET /api/v1/sites/{site_id}/stats`` (AP, switch, gateway and
    client counts), ``.../stats/devices?type=all`` (each device's status)
    and ``.../alarms/search`` (the most severe, newest alarms of the last
    ``alarm_duration``). A part that fails carries its own ``error`` and
    ``degraded`` is true; the rest still comes back.
    """
    site = seg(site_id)
    top = clamp_limit(top_alarms, default=5)
    stats_call = get(f"/api/v1/sites/{site}/stats", site_id=site_id)
    devices_call = get(f"/api/v1/sites/{site}/stats/devices", {"type": "all", "limit": 1000}, site_id=site_id)
    alarms_call = get(
        f"/api/v1/sites/{site}/alarms/search",
        {"duration": alarm_duration, "limit": 100, "sort": "-timestamp"},
        site_id=site_id,
    )
    stats, devices, alarms = await asyncio.gather(_section(stats_call), _section(devices_call), _section(alarms_call))
    out: dict[str, Any] = {"site_id": site_id}
    out["site"] = stats if _failed(stats) else pick(stats, _SITE_STATS_FIELDS)
    out["devices"] = devices if _failed(devices) else _device_counts(extract_items(devices))
    if _failed(alarms):
        out["top_alarms"] = alarms
    else:
        items = [a for a in extract_items(alarms) if isinstance(a, dict)]
        by_severity: dict[str, int] = {}
        for alarm in items:
            sev = str(alarm.get("severity") or "unknown")
            by_severity[sev] = by_severity.get(sev, 0) + 1
        items.sort(key=lambda a: (_SEVERITY_RANK.get(str(a.get("severity")), 3), -float(a.get("timestamp") or 0)))
        out["alarms_by_severity"] = by_severity
        out["top_alarms"] = [pick(a, _ALARM_FIELDS) for a in items[:top]]
    out["degraded"] = any(_failed(part) for part in (stats, devices, alarms))
    return out


def _failed(part: Any) -> bool:
    return isinstance(part, dict) and "error" in part


@mcp.tool()
async def mist_list_site_clients(
    site_id: str,
    ssid: str | None = None,
    hostname: str | None = None,
    username: str | None = None,
    mac: str | None = None,
    ip: str | None = None,
    text: str | None = None,
    duration: str = "1d",
    limit: int = 100,
    search_after: str | None = None,
) -> dict[str, Any]:
    """List the wireless clients a site has seen, optionally on one SSID.

    Uses ``GET /api/v1/sites/{site_id}/clients/search`` over the last
    ``duration`` (``1h``, ``1d``). Filters: ``ssid``, ``hostname``,
    ``username``, ``mac``, ``ip`` and free ``text``. Pass ``search_after``
    from a previous reply to continue.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {
        "ssid": ssid,
        "hostname": hostname,
        "username": username,
        "mac": normalize_mac(mac) if mac else None,
        "ip": ip,
        "text": text,
        "duration": duration,
        "limit": safe_limit,
        "search_after": search_after,
    }
    data = await get(f"/api/v1/sites/{seg(site_id)}/clients/search", params, site_id=site_id)
    out: dict[str, Any] = {"clients": _bounded(extract_items(data), _CLIENT_FIELDS, safe_limit)}
    if isinstance(data, dict) and data.get("next"):
        out["next"] = data["next"]
    return out


# ── NAC diagnosis ──────────────────────────────────────────────────────────

#: Spec ``psk``: the fields that say whether a cloud PSK is bound to an SSID.
_BOUND_PSK_FIELDS = ("id", "name", "usage", "vlan_id", "ssid")
#: Spec ``nac_client``: the decision, without the full record.
_NAC_CLIENT_SUMMARY_FIELDS = (
    "mac", "username", "ssid", "auth_type", "type", "random_mac", "vlan", "last_status",
    "nacrule_matched", "nacrule_name", "group", "resp_attrs",
)  # fmt: skip
#: Spec ``nac_event``: the decision, without the whole event.
_NAC_EVENT_SUMMARY_FIELDS = (
    "type", "mac", "ssid", "auth_type", "nacrule_name", "nacrule_matched", "group", "resp_attrs", "text",
)  # fmt: skip
#: Spec ``nac_rule``: what decides whether the rule can match at all.
_NAC_RULE_SUMMARY_FIELDS = ("id", "name", "action", "enabled", "order", "matching")
#: Spec ``nac_auth_type``. A rule whose ``matching.auth_type`` is not one of
#: these (for example ``psk`` or ``psk-mab``) can never match a request.
_NAC_RULE_AUTH_TYPES = frozenset({"cert", "device-auth", "eap-teap", "eap-tls", "eap-ttls", "idp", "mab", "eap-peap"})


@mcp.tool()
async def mist_wlan_security_summary(org_id: str, wlan_id: str) -> dict[str, Any]:
    """One WLAN's security in a few fields, and whether it is doing MPSK.

    Reads ``GET /api/v1/orgs/{org_id}/wlans/{wlan_id}`` and the org's PSKs.
    ``cloud_psks_bound`` is the decisive switch that makes Mist NAC run the
    multi-PSK lookup, where an unknown MAC gets
    ``NAC_CLIENT_PPSK_KEY_NOT_FOUND`` ("Un-registered client and use default
    passphrase to get access") instead of being authorised by a NAC rule.
    ``dynamic_psk_present`` says the WLAN has a dynamic-PSK block, but its
    value is redacted before a tool sees it, so ``dynamic_psk.enabled`` cannot
    be read here. ``verdict`` says what that means for allow-all MAB.
    """
    data = await get(f"/api/v1/orgs/{seg(org_id)}/wlans/{seg(wlan_id)}", org_id=org_id)
    if not isinstance(data, dict):
        return {"error": "WLAN not found or not readable."}
    auth_raw = data.get("auth")
    auth: dict[str, Any] = auth_raw if isinstance(auth_raw, dict) else {}
    nac_raw = data.get("mist_nac")
    nac: dict[str, Any] = nac_raw if isinstance(nac_raw, dict) else {}
    dynamic_present = "dynamic_psk" in data and data.get("dynamic_psk") is not None
    ssid = data.get("ssid")
    psks = await get(f"/api/v1/orgs/{seg(org_id)}/psks", {"limit": 1000}, org_id=org_id)
    bound = [
        pick(item, _BOUND_PSK_FIELDS)
        for item in extract_items(psks)
        if isinstance(item, dict) and item.get("ssid") == ssid
    ]
    if bound:
        verdict = (
            "MPSK active: cloud PSKs are bound to this SSID, so unknown MACs hit the multi-PSK lookup and "
            "NAC policy rules are not consulted. Detach them for MAB allow-all."
        )
    elif dynamic_present:
        verdict = (
            "No cloud PSKs bound, but this WLAN has a dynamic_psk block (its value is hidden from tools). "
            "If it is enabled it also drives MPSK; check the Mist UI."
        )
    elif nac.get("enabled"):
        verdict = "Mist NAC MAB: unknown MACs fall through to the org's NAC rules."
    else:
        verdict = "Mist NAC is off; RADIUS uses auth_servers (or the WLAN is open)."
    return {
        "org_id": org_id,
        "wlan_id": wlan_id,
        "ssid": ssid,
        "enabled": data.get("enabled"),
        "auth_type": auth.get("type"),
        "enable_mac_auth": auth.get("enable_mac_auth"),
        "mist_nac_enabled": nac.get("enabled"),
        "dynamic_psk_present": dynamic_present,
        "cloud_psks_bound": bound,
        "mpsk_active": bool(bound),
        "vlan_id": data.get("vlan_id"),
        "template_id": data.get("template_id"),
        "verdict": verdict,
    }


@mcp.tool()
async def mist_nac_troubleshoot(
    org_id: str,
    site_id: str,
    mac: str | None = None,
    ssid: str | None = None,
    duration: str = "1d",
    limit: int = 100,
) -> dict[str, Any]:
    """Why a wireless client did, or did not, get through Mist Access Assurance.

    Joins the site's NAC clients (``.../nac_clients/search``), their recent NAC
    events (``.../nac_clients/events/search``) and the org's NAC rules
    (``GET /api/v1/orgs/{org_id}/nacrules``), then answers in ``verdict``: the
    multi-PSK lookup intercepting (``NAC_CLIENT_PPSK_KEY_NOT_FOUND``), no rule
    matching (``NAC_CLIENT_DENY`` with ``Match Not Found``), or a rule allowing
    the client (``NAC_CLIENT_PERMIT``). ``rules_with_unknown_auth_type`` lists
    rules whose ``matching.auth_type`` Mist will never match.
    """
    safe_limit = clamp_limit(limit, default=100)
    org = seg(org_id)
    site = seg(site_id)
    norm = normalize_mac(mac) if mac else None
    clients_data, events_data, rules_data = await asyncio.gather(
        _section(get(f"/api/v1/sites/{site}/nac_clients/search", {"limit": safe_limit, "ssid": ssid}, site_id=site_id)),
        _section(
            get(
                f"/api/v1/sites/{site}/nac_clients/events/search",
                {"duration": duration, "limit": safe_limit, "mac": norm, "ssid": ssid},
                site_id=site_id,
            )
        ),
        _section(get(f"/api/v1/orgs/{org}/nacrules", org_id=org_id)),
    )
    out: dict[str, Any] = {"org_id": org_id, "site_id": site_id}
    if _failed(clients_data):
        out["clients"] = clients_data
    else:
        clients = [c for c in extract_items(clients_data) if isinstance(c, dict)]
        if norm:
            clients = [c for c in clients if str(c.get("mac") or "").lower() == norm]
        out["clients"] = _bounded(clients, _NAC_CLIENT_SUMMARY_FIELDS, safe_limit)
    counts: dict[str, int] = {}
    if _failed(events_data):
        out["events"] = events_data
    else:
        events = [e for e in extract_items(events_data) if isinstance(e, dict)]
        if norm:
            events = [e for e in events if str(e.get("mac") or "").lower() == norm]
        for event in events:
            key = str(event.get("type") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        out["event_counts"] = counts
        out["recent_events"] = [pick(e, _NAC_EVENT_SUMMARY_FIELDS) for e in events[:20]]
    if not _failed(rules_data):
        rules = [r for r in extract_items(rules_data) if isinstance(r, dict)]
        out["rules"] = [pick(r, _NAC_RULE_SUMMARY_FIELDS) for r in rules]
        unknown = []
        for rule in rules:
            matching_raw = rule.get("matching")
            matching: dict[str, Any] = matching_raw if isinstance(matching_raw, dict) else {}
            auth_type = matching.get("auth_type")
            if isinstance(auth_type, str) and auth_type not in _NAC_RULE_AUTH_TYPES:
                unknown.append(rule.get("name"))
        if unknown:
            out["rules_with_unknown_auth_type"] = unknown
    verdicts = []
    if "NAC_CLIENT_PPSK_KEY_NOT_FOUND" in counts:
        verdicts.append(
            "MPSK lookup intercepting: an unknown MAC got the multi-PSK reply; see "
            "mist_wlan_security_summary and detach the SSID's cloud PSKs."
        )
    if "NAC_CLIENT_DENY" in counts:
        verdicts.append(
            "A deny was seen; if its resp_attrs is 'Match Not Found', no auth-policy rule matched "
            "(check rules_with_unknown_auth_type and rule order)."
        )
    if "NAC_CLIENT_PERMIT" in counts:
        verdicts.append("A permit was seen: an auth-policy rule allowed the client.")
    if not counts and not _failed(events_data):
        verdicts.append("No NAC events in the window.")
    out["verdict"] = " ".join(verdicts) or None
    out["degraded"] = any(_failed(part) for part in (clients_data, events_data, rules_data))
    return out


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
