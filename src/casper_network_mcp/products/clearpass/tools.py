"""ClearPass curated tools, and the ``clearpass`` backend that holds them.

Adapted from hpe-networking-mcp ``mcp_servers/clearpass.py`` (MIT,
nowireless4u/hpe-networking-mcp). Paths were checked against the bundled
ClearPass documents (served under ``/api``). What changed:

* every request goes through the gated ClearPass client (the read-only pin
  first, then the path check, then the login);
* every id in a path goes through ``path_segment`` (``/ ? # %`` and ``..`` are
  refused before anything is sent);
* no ``confirm`` argument and no write switch: approval is Casper's box.
  Change tools take ``dry_run: bool = False``; with ``dry_run=True`` they
  return ``{"would_send": {method, path, body}}`` and send nothing;
* the generic ``clearpass_write`` passthrough is gone (each endpoint has its
  own generated tool with an honest change kind); ``clearpass_get`` stays,
  for reads only; ``clearpass_status`` is gone (``access_check`` reports the
  login);
* spec fixes: the endpoint attribute PATCH sends no ``change_of_authorization``
  (the spec has none on that operation), the cluster server list sends no
  paging (the spec has none; it is bounded here), and list replies are read
  from ``_embedded.items``;
* new: ``clearpass_who_am_i`` (``GET /api/oauth/me``), the login's own
  identity, which ``access_check`` builds on.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.budget import bound_collection_response, clamp_limit
from casper_network_mcp.core.paths import path_segment, safe_api_path
from casper_network_mcp.products._tools import ClientGetter, ToolSet, client, use_client, would_send

__all__ = ["backend", "backend_tools"]

mcp = ToolSet("clearpass")

_ENDPOINT_FIELDS = (
    "id", "mac_address", "status", "enabled", "profile_status", "profile", "profile_name", "device_category",
    "device_family", "device_name", "hostname", "ip_address", "username", "description", "updated_at",
    "created_at", "last_seen",
)  # fmt: skip
_SESSION_FIELDS = (
    "id", "session_id", "username", "user_name", "mac_address", "calling_station_id", "endpoint_mac_address",
    "nasipaddress", "nas_ip", "nas_identifier", "nas_port_id", "auth_status", "service_name",
    "enforcement_profile", "acctstarttime", "timestamp",
)  # fmt: skip
_SESSION_REASONS = ("reason", "auth_error", "error_message", "reply_message", "alert_message", "result")
_NAD_FIELDS = ("id", "name", "ip_address", "vendor_name", "coa_capable", "coa_port", "status", "enabled", "description")
_GUEST_FIELDS = (
    "id", "username", "email", "visitor_name", "name", "role_name", "sponsor_name", "sponsor_email", "enabled",
    "expire_time", "created_at", "updated_at",
)  # fmt: skip


def _cp() -> Any:
    return client("clearpass")


def _seg(value: str) -> str:
    return path_segment(value)


def _normalize_mac(mac_address: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", mac_address or "").lower()
    if len(normalized) != 12:
        raise ValueError("MAC address must contain exactly 12 hex characters")
    return normalized


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    clean = {k: v for k, v in (params or {}).items() if v is not None} or None
    return await _cp().request("GET", path, params=clean, kind="read")


async def _change(
    method: str,
    path: str,
    *,
    body: Any = None,
    params: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> Any:
    if dry_run:
        return would_send(method, path, body, params)
    out = await _cp().request(method, path, params=params or None, json=body)
    return out if out is not None else {"ok": True}


def _items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    embedded = data.get("_embedded")
    if isinstance(embedded, dict) and isinstance(embedded.get("items"), list):
        return list(embedded["items"])
    for key in ("items", "results", "data", "_embedded"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _first(data: Any) -> Any:
    if isinstance(data, dict) and not _items(data):
        return data
    items = _items(data)
    return items[0] if items else None


def _pick(data: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return data
    return {field: data[field] for field in fields if field in data and data[field] not in (None, "")}


def _session(session: Any) -> Any:
    out = _pick(session, _SESSION_FIELDS)
    if isinstance(session, dict):
        for key in _SESSION_REASONS:
            if session.get(key):
                out["reason"] = session[key]
                break
    return out


def _page(limit: int, offset: int, default: int = 25) -> tuple[int, dict[str, Any]]:
    safe_limit = clamp_limit(limit, default=default)
    return safe_limit, {"offset": max(0, offset), "limit": safe_limit, "calculate_count": "false"}


def _listed(items: list[Any], limit: int, offset: int) -> Any:
    out = bound_collection_response(items, limit=limit, offset=0)
    if isinstance(out, dict):
        out["server_offset"] = max(0, offset)
    return out


# ── reads ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def clearpass_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """Read any ClearPass API path with GET (for endpoints no other tool covers).

    Only ``/api/...`` paths. Lists are bounded with ``limit`` and ``offset``;
    secret values are hidden.
    """
    safe_api_path(path, ("/api/",))
    data = await _cp().request("GET", path, params=params or None, kind="read")
    items = _items(data) if isinstance(data, dict) and "_embedded" in data else None
    if items is not None and isinstance(data, dict):
        rest = {k: v for k, v in data.items() if k != "_embedded"}
        return {**rest, **bound_collection_response(items, limit=clamp_limit(limit), offset=max(0, offset))}
    return bound_collection_response(data, limit=clamp_limit(limit), offset=max(0, offset))


@mcp.tool()
async def clearpass_who_am_i() -> dict[str, Any]:
    """The ClearPass login this server uses: its API client and user (``GET /api/oauth/me``)."""
    return {"me": await _get("/api/oauth/me")}


@mcp.tool()
async def clearpass_get_endpoint_by_mac(mac_address: str) -> dict[str, Any]:
    """Look up one endpoint by MAC (``GET /api/endpoint/mac-address/{mac}``): profile and status.

    Accepts colon, dash, dotted or compact MACs.
    """
    normalized = _normalize_mac(mac_address)
    data = await _get(f"/api/endpoint/mac-address/{normalized}", {"profile_details": "true"})
    return {"normalized_mac": normalized, "endpoint": _pick(_first(data), _ENDPOINT_FIELDS)}


@mcp.tool()
async def clearpass_list_auth_failures(limit: int = 25, offset: int = 0, auth_status: str = "FAILED") -> dict[str, Any]:
    """Recent authentication failures from Access Tracker (``GET /api/session``), newest first.

    Filters on ``auth_status`` (default FAILED). Returns username, MAC, NAD,
    status and the reason.
    """
    safe_limit, params = _page(limit, offset)
    params.update({"filter": json.dumps({"auth_status": auth_status}, separators=(",", ":")), "sort": "-acctstarttime"})
    data = await _get("/api/session", params)
    sessions = _listed([_session(item) for item in _items(data)], safe_limit, offset)
    if isinstance(sessions, dict):
        sessions["filter"] = {"auth_status": auth_status}
    return {"sessions": sessions}


@mcp.tool()
async def clearpass_get_network_device(name: str | None = None, device_id: str | None = None) -> dict[str, Any]:
    """One network device (NAD) by name or numeric id: RADIUS/TACACS and CoA settings.

    Uses ``GET /api/network-device/name/{name}`` or ``/api/network-device/{id}``.
    """
    if bool(name) == bool(device_id):
        raise ValueError("Provide exactly one of name or device_id.")
    path = f"/api/network-device/name/{_seg(name)}" if name else f"/api/network-device/{_seg(device_id or '')}"
    return {"network_device": _pick(_first(await _get(path)), _NAD_FIELDS)}


@mcp.tool()
async def clearpass_find_guest(
    query: str,
    field: Literal["username", "email", "visitor_name", "name"] = "username",
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Find guest accounts by username, email, visitor_name or name.

    ``username`` uses ``GET /api/guest/username/{username}``; other fields
    query ``GET /api/guest`` with a filter.
    """
    if field == "username":
        data = await _get(f"/api/guest/username/{_seg(query)}")
        return {"guests": bound_collection_response([_pick(_first(data), _GUEST_FIELDS)], limit=1, offset=0)}
    safe_limit, params = _page(limit, offset)
    params["filter"] = json.dumps({field: query}, separators=(",", ":"))
    guests = _listed(
        [_pick(item, _GUEST_FIELDS) for item in _items(await _get("/api/guest", params))], safe_limit, offset
    )
    if isinstance(guests, dict):
        guests["filter"] = {field: query}
    return {"guests": guests}


@mcp.tool()
async def clearpass_get_insight_endpoint(mac_address: str) -> dict[str, Any]:
    """Insight data for one endpoint MAC (``GET /api/insight/endpoint/mac/{mac}``)."""
    normalized = _normalize_mac(mac_address)
    return {"normalized_mac": normalized, "endpoint": _first(await _get(f"/api/insight/endpoint/mac/{normalized}"))}


@mcp.tool()
async def clearpass_list_onguard_activity(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """OnGuard (posture agent) activity records (``GET /api/onguard-activity``)."""
    safe_limit, params = _page(limit, offset)
    return {"activity": _listed(_items(await _get("/api/onguard-activity", params)), safe_limit, offset)}


@mcp.tool()
async def clearpass_get_onguard_activity_by_mac(mac_address: str) -> dict[str, Any]:
    """OnGuard activity for one endpoint MAC (``GET /api/onguard-activity/host_mac/{mac}``)."""
    normalized = _normalize_mac(mac_address)
    return {"normalized_mac": normalized, "activity": await _get(f"/api/onguard-activity/host_mac/{normalized}")}


@mcp.tool()
async def clearpass_list_access_tracker_sessions(
    status: str | None = None, limit: int = 25, offset: int = 0
) -> dict[str, Any]:
    """Access Tracker sessions, newest first (``GET /api/session``), optionally by ``auth_status``.

    ``status``: for example FAILED, REJECT, ALLOW, DISCONNECT. For failures
    only, ``clearpass_list_auth_failures`` is simpler.
    """
    safe_limit, params = _page(limit, offset)
    params["sort"] = "-acctstarttime"
    if status is not None:
        params["filter"] = json.dumps({"auth_status": status}, separators=(",", ":"))
    sessions = _listed([_session(item) for item in _items(await _get("/api/session", params))], safe_limit, offset)
    if isinstance(sessions, dict) and status is not None:
        sessions["filter"] = {"auth_status": status}
    return {"sessions": sessions}


@mcp.tool()
async def clearpass_get_access_tracker_session(session_id: str) -> dict[str, Any]:
    """One Access Tracker session by id (``GET /api/session/{id}``)."""
    return {"session": _session(await _get(f"/api/session/{_seg(session_id)}"))}


@mcp.tool()
async def clearpass_list_endpoints(limit: int = 25, offset: int = 0, status: str | None = None) -> dict[str, Any]:
    """Endpoints, optionally by status (Known, Unknown, Disabled) (``GET /api/endpoint``)."""
    safe_limit, params = _page(limit, offset)
    if status is not None:
        params["filter"] = json.dumps({"status": status}, separators=(",", ":"))
    params["profile_details"] = "false"  # required by the spec; profiles come from get_endpoint_by_mac
    items = [_pick(item, _ENDPOINT_FIELDS) for item in _items(await _get("/api/endpoint", params))]
    return {"endpoints": _listed(items, safe_limit, offset)}


@mcp.tool()
async def clearpass_list_guests(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """Guest accounts (``GET /api/guest``). To search, use ``clearpass_find_guest``."""
    safe_limit, params = _page(limit, offset)
    items = [_pick(item, _GUEST_FIELDS) for item in _items(await _get("/api/guest", params))]
    return {"guests": _listed(items, safe_limit, offset)}


@mcp.tool()
async def clearpass_list_roles(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """ClearPass roles (``GET /api/role``)."""
    safe_limit, params = _page(limit, offset)
    return {"roles": _listed(_items(await _get("/api/role", params)), safe_limit, offset)}


@mcp.tool()
async def clearpass_list_enforcement_policies(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """Enforcement policies (``GET /api/enforcement-policy``)."""
    safe_limit, params = _page(limit, offset)
    return {"enforcement_policies": _listed(_items(await _get("/api/enforcement-policy", params)), safe_limit, offset)}


@mcp.tool()
async def clearpass_get_enforcement_policy(name: str) -> dict[str, Any]:
    """One enforcement policy by name (``GET /api/enforcement-policy/name/{name}``)."""
    return {"enforcement_policy": await _get(f"/api/enforcement-policy/name/{_seg(name)}")}


@mcp.tool()
async def clearpass_list_services(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """Authentication services (``GET /api/config/service``)."""
    safe_limit, params = _page(limit, offset)
    return {"services": _listed(_items(await _get("/api/config/service", params)), safe_limit, offset)}


@mcp.tool()
async def clearpass_get_service(name: str) -> dict[str, Any]:
    """One authentication service by name (``GET /api/config/service/name/{name}``)."""
    return {"service": await _get(f"/api/config/service/name/{_seg(name)}")}


@mcp.tool()
async def clearpass_list_syslog_targets(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """Syslog targets (``GET /api/syslog-target``)."""
    safe_limit, params = _page(limit, offset)
    return {"syslog_targets": _listed(_items(await _get("/api/syslog-target", params)), safe_limit, offset)}


@mcp.tool()
async def clearpass_list_syslog_export_filters(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """Syslog export filters (``GET /api/syslog-export-filter``)."""
    safe_limit, params = _page(limit, offset)
    items = _items(await _get("/api/syslog-export-filter", params))
    return {"syslog_export_filters": _listed(items, safe_limit, offset)}


@mcp.tool()
async def clearpass_get_server_version() -> dict[str, Any]:
    """The ClearPass server version (``GET /api/server/version``)."""
    return {"version": await _get("/api/server/version")}


@mcp.tool()
async def clearpass_list_cluster_servers(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """The cluster's server nodes (``GET /api/cluster/server``; the spec has no paging, so the list is bounded here)."""
    items = _items(await _get("/api/cluster/server"))
    return {
        "cluster_servers": bound_collection_response(items, limit=clamp_limit(limit, default=25), offset=max(0, offset))
    }


# ── changes ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def clearpass_update_endpoint_attributes(
    mac_address: str, attributes: dict[str, Any], dry_run: bool = False
) -> Any:
    """Change an endpoint's attributes by MAC (``PATCH /api/endpoint/mac-address/{mac}``).

    With ``dry_run=True`` it returns the request it would send and sends nothing.
    """
    normalized = _normalize_mac(mac_address)
    out = await _change(
        "PATCH", f"/api/endpoint/mac-address/{normalized}", body={"attributes": attributes}, dry_run=dry_run
    )
    return {**out, "normalized_mac": normalized} if isinstance(out, dict) else out


@mcp.tool()
async def clearpass_delete_endpoint(mac_address: str, dry_run: bool = False) -> Any:
    """Delete an endpoint by MAC (``DELETE /api/endpoint/mac-address/{mac}``)."""
    normalized = _normalize_mac(mac_address)
    out = await _change("DELETE", f"/api/endpoint/mac-address/{normalized}", dry_run=dry_run)
    return {**out, "normalized_mac": normalized} if isinstance(out, dict) else out


def _guest_path(username: str | None, guest_id: str | None) -> str:
    if bool(username) == bool(guest_id):
        raise ValueError("Provide exactly one of username or guest_id.")
    return f"/api/guest/username/{_seg(username)}" if username else f"/api/guest/{_seg(guest_id or '')}"


@mcp.tool()
async def clearpass_set_guest_enabled(
    enabled: bool,
    username: str | None = None,
    guest_id: str | None = None,
    change_of_authorization: bool = False,
    dry_run: bool = False,
) -> Any:
    """Turn a guest account on or off, by username or id (``PATCH /api/guest/...``).

    ``change_of_authorization=True`` also re-authorises the guest's live
    sessions (they may drop).
    """
    params = {"change_of_authorization": str(change_of_authorization).lower()}
    return await _change(
        "PATCH", _guest_path(username, guest_id), body={"enabled": enabled}, params=params, dry_run=dry_run
    )


@mcp.tool()
async def clearpass_delete_guest(
    username: str | None = None, guest_id: str | None = None, dry_run: bool = False
) -> Any:
    """Delete a guest account by username or id (``DELETE /api/guest/...``)."""
    return await _change("DELETE", _guest_path(username, guest_id), dry_run=dry_run)


@mcp.tool()
async def clearpass_create_guest(
    username: str, password: str | None = None, role_name: str | None = None, dry_run: bool = False
) -> Any:
    """Create a guest account (``POST /api/guest``). The password is hidden in previews and replies."""
    body: dict[str, Any] = {"username": username}
    if password is not None:
        body["password"] = password
    if role_name is not None:
        body["role_name"] = role_name
    return await _change("POST", "/api/guest", body=body, dry_run=dry_run)


@mcp.tool()
async def clearpass_disconnect_session(session_id: str, dry_run: bool = False) -> Any:
    """Disconnect a live session with a change of authorisation (``POST /api/session/{id}/disconnect``)."""
    return await _change("POST", f"/api/session/{_seg(session_id)}/disconnect", dry_run=dry_run)


@mcp.tool()
async def clearpass_set_service_enabled(name: str, enabled: bool, dry_run: bool = False) -> Any:
    """Turn an authentication service on or off by name.

    ``PATCH /api/config/service/name/{name}/enable`` or ``.../disable``. A
    disabled service stops matching requests, so authentications it handled
    fall to other services or fail.
    """
    action = "enable" if enabled else "disable"
    return await _change("PATCH", f"/api/config/service/name/{_seg(name)}/{action}", dry_run=dry_run)


# ── the backend ─────────────────────────────────────────────────────────────


def backend(client: ClientGetter | None = None) -> MCPServer:
    """The ``clearpass`` backend: every hand-written ClearPass tool, labelled from ``labels.yaml``."""
    if client is not None:
        use_client("clearpass", client)
    server = MCPServer("clearpass")
    mcp.register(server)
    return server


def backend_tools() -> list[Any]:
    """The registered ClearPass tools (``.name``, ``.fn``, ``.parameters``)."""
    return list(sdk_compat.tool_registry(backend()).values())
