"""ClearPass curated tools, and the ``clearpass`` backend that holds them.

Adapted from hpe-networking-mcp ``mcp_servers/clearpass.py``. Paths were checked against the bundled
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
  for plain reads only (a GET listed in ``KIND_OVERRIDES``, such as a guest
  receipt, is refused); ``clearpass_status`` is gone (``access_check`` reports the
  login);
* spec fixes: the endpoint attribute PATCH sends no ``change_of_authorization``
  (the spec has none on that operation), the cluster server list sends no
  paging (the spec has none; it is bounded here), list replies are read
  from ``_embedded.items``, a new guest gets the ``role_id`` and password the
  spec requires, and a disconnect sends ``id`` and ``confirm_disconnect``;
* new: ``clearpass_who_am_i`` (``GET /api/oauth/me``), the login's own
  identity, which ``access_check`` builds on.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.budget import bound_collection_response, clamp_limit
from casper_network_mcp.core.kinds import KIND_OVERRIDES, kind_for_operation, listed_template
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
    "enforcement_profile", "acctstarttime", "timestamp", "acctsessionid", "callingstationid", "nas_name",
    "nasportid", "servicetype", "state", "acctstoptime", "acctterminatecause", "tipsrole", "arubauserrole", "role_name",
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


MacFilter = Annotated[
    str | None,
    Field(description="Client MAC: 12 hex digits, colon/dash octets or dotted groups; e.g. 02:00:00:00:00:01."),
]
UsernameFilter = Annotated[str | None, Field(description="Exact session username; must not be blank.")]
NadFilter = Annotated[str | None, Field(description="Exact NAD IP address (session nasipaddress).")]
ServiceFilter = Annotated[
    str | None,
    Field(description="Exact accounting service type (servicetype), not an Access Tracker policy service name."),
]
StartFilter = Annotated[
    str | None,
    Field(description="Inclusive session start-time lower bound, ISO 8601 with timezone, e.g. 2025-01-01T00:00:00Z."),
]
EndFilter = Annotated[
    str | None, Field(description="Inclusive session start-time upper bound, ISO 8601 with timezone; must be >= start.")
]


def _session_mac(value: str) -> str:
    if not re.fullmatch(
        r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|"
        r"(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4})",
        value,
    ):
        raise ValueError("mac must be 12 hex digits, colon/dash-separated octets or dotted groups (02:00:00:00:00:01).")
    return _normalize_mac(value)


def _iso_time(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if "T" not in value or parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an ISO 8601 date-time with timezone, e.g. 2025-01-01T00:00:00Z.") from None


async def _session_list(
    status: str | None,
    limit: int,
    offset: int,
    mac: str | None,
    username: str | None,
    nad: str | None,
    service: str | None,
    start: str | None,
    end: str | None,
) -> dict[str, Any]:
    lower, upper = _iso_time(start, "start"), _iso_time(end, "end")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("start must be before or equal to end.")
    filters = {
        k: v
        for k, v in {
            "auth_status": status,
            "mac_address": _session_mac(mac) if mac is not None else None,
            "username": username,
            "nasipaddress": nad,
            "servicetype": service,
        }.items()
        if v is not None
    }
    if any(not v.strip() for v in filters.values()):
        raise ValueError("Session filters must not be blank.")
    safe_limit, params = _page(limit, offset)
    params["sort"] = "-acctstarttime"
    if filters:
        params["filter"] = json.dumps(filters, separators=(",", ":"))
    sessions: list[Any] = []
    next_offset = max(0, offset)
    has_more = False
    missing_times = 0
    # The spec declares a JSON filter but no range operators: compare times locally.
    for _ in range(10 if lower is not None or upper is not None else 1):
        params["offset"] = next_offset
        rows = _items(await _get("/api/session", params))
        consumed = 0
        for row in rows:
            consumed += 1
            if not isinstance(row, dict):
                continue
            if lower is not None or upper is not None:
                try:
                    stamp = _iso_time(row.get("acctstarttime"), "acctstarttime")
                except ValueError:
                    stamp = None
                if stamp is None:
                    missing_times += 1
                    continue
                if (lower is not None and stamp < lower) or (upper is not None and stamp > upper):
                    continue
            sessions.append(_session(row))
            if len(sessions) >= safe_limit:
                break
        next_offset += consumed
        has_more = consumed < len(rows) or len(rows) >= safe_limit
        if not has_more or len(sessions) >= safe_limit:
            break
    out = _listed(sessions, safe_limit, offset)
    out["filter"] = filters
    if lower is not None or upper is not None:
        out["time_window"] = {"start": start, "end": end, "unusable_timestamps": missing_times}
    if has_more:
        out["next_server_offset"] = next_offset
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

    Only ``/api/...`` paths, and only GETs that are plain reads (a guest
    receipt can hold a password, so it is refused here). Lists are bounded
    with ``limit`` and ``offset``; secret values are hidden.
    """
    safe = safe_api_path(path, ("/api/",))
    if kind_for_operation("GET", safe) != "read":
        template = listed_template("GET", safe) or safe
        why = KIND_OVERRIDES.get(("GET", template), ("", "it is not a plain read"))[1]
        return {
            "error": f"Not sent: GET {safe} is not a plain read ({why}), so clearpass_get does not run it. "
            f"Find its own tool with find_tool('GET {template}') and run that with invoke_tool."
        }
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
async def clearpass_list_auth_failures(
    limit: Annotated[int, Field(description="Sessions per page; clamped to 1..200.")] = 25,
    offset: Annotated[int, Field(description="Server offset, or next_server_offset from an earlier scan.")] = 0,
    auth_status: Annotated[
        str, Field(description="Exact auth_status filter (default FAILED); not declared by the active-session schema.")
    ] = "FAILED",
    mac: MacFilter = None,
    username: UsernameFilter = None,
    nad: NadFilter = None,
    service: ServiceFilter = None,
    start: StartFilter = None,
    end: EndFilter = None,
) -> dict[str, Any]:
    """Recent authentication failures from Access Tracker (``GET /api/session``), newest first.

    Filters on ``auth_status`` (default FAILED). Returns username, MAC, NAD,
    status and the reason when supplied. The bundled active-session schema
    does not declare auth_status or reject reasons. Time windows scan at most
    ten pages locally; pass next_server_offset as offset to continue.
    """
    return {"sessions": await _session_list(auth_status, limit, offset, mac, username, nad, service, start, end)}


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
    status: Annotated[
        str | None, Field(description="Optional auth_status filter; not declared by the active-session schema.")
    ] = None,
    limit: Annotated[int, Field(description="Sessions per page; clamped to 1..200.")] = 25,
    offset: Annotated[int, Field(description="Server offset, or next_server_offset from an earlier scan.")] = 0,
    mac: MacFilter = None,
    username: UsernameFilter = None,
    nad: NadFilter = None,
    service: ServiceFilter = None,
    start: StartFilter = None,
    end: EndFilter = None,
) -> dict[str, Any]:
    """Access Tracker sessions, newest first (``GET /api/session``), optionally by ``auth_status``.

    The bundled spec describes active accounting sessions, not full Access
    Tracker records. Time bounds apply to acctstarttime locally, scanning at
    most ten pages. If next_server_offset is present, pass it as offset to
    continue (even if this page has no matches). Invalid timestamps are counted.
    """
    return {"sessions": await _session_list(status, limit, offset, mac, username, nad, service, start, end)}


@mcp.tool()
async def clearpass_why_denied(
    mac: MacFilter = None,
    username: UsernameFilter = None,
    nad: NadFilter = None,
    service: ServiceFilter = None,
    start: StartFilter = None,
    end: EndFilter = None,
    limit: Annotated[int, Field(description="Latest sessions to summarize; clamped to 1..200.")] = 5,
    offset: Annotated[int, Field(description="Server offset; use next_server_offset to continue scanning.")] = 0,
) -> dict[str, Any]:
    """Why was this client (MAC or username) denied on ClearPass? Failure reason when reported, recent sessions, endpoint profile, NAD and roles.

    Provide exactly one of mac or username. Reads GET /api/session and
    GET /api/endpoint/mac-address/{mac_address}. The bundled 6.12.7 API has
    active accounting sessions, not full Access Tracker rejects: missing auth
    result, failure reason or enforcement profile is unknown, never inferred
    from accounting state or termination cause. Service is the accounting type.
    For a username, endpoint details describe only the latest session's MAC.
    Time windows scan at most ten pages; next_server_offset continues the scan.
    """
    if (mac is None) == (username is None):
        raise ValueError("Provide exactly one of mac or username.")
    page = await _session_list(None, limit, offset, mac, username, nad, service, start, end)
    rows = page["items"]
    normalized = _session_mac(mac) if mac is not None else None
    if normalized is None and rows:
        candidate = rows[0].get("mac_address") or rows[0].get("callingstationid") or rows[0].get("calling_station_id")
        if isinstance(candidate, str):
            try:
                normalized = _session_mac(candidate)
            except ValueError:
                pass
    endpoint: Any = None
    if normalized is not None:
        endpoint_data = await _part(_get(f"/api/endpoint/mac-address/{normalized}", {"profile_details": "true"}))
        endpoint = endpoint_data if _failed(endpoint_data) else _pick(_first(endpoint_data), _ENDPOINT_FIELDS)
    summaries = [
        {
            "id": row.get("id") or row.get("session_id"),
            "time": row.get("acctstarttime") or row.get("timestamp"),
            "username": row.get("username") or row.get("user_name"),
            "mac": row.get("mac_address") or row.get("callingstationid") or row.get("calling_station_id"),
            "service": row.get("service_name") or row.get("servicetype"),
            "nad": {
                "ip": row.get("nasipaddress") or row.get("nas_ip"),
                "name": row.get("nas_name") or row.get("nas_identifier"),
            },
            "auth_result": row.get("auth_status"),
            "reason": row.get("reason"),
            "enforcement_profile": row.get("enforcement_profile"),
            "roles": _pick(row, ("tipsrole", "arubauserrole", "role_name")),
            "accounting_state": row.get("state"),
            "termination_cause": row.get("acctterminatecause"),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    out: dict[str, Any] = {
        "sessions": summaries,
        "endpoint_mac": normalized,
        "endpoint": endpoint,
        "filter": page["filter"],
        "server_offset": page["server_offset"],
        "degraded": _failed(endpoint),
        "limitation": "The bundled API exposes active accounting sessions, not full Access Tracker rejects. "
        "Missing auth results, reject reasons and enforcement profiles are unknown; no denial is inferred.",
    }
    for key in ("next_server_offset", "time_window"):
        if key in page:
            out[key] = page[key]
    if not summaries:
        out["message"] = "No matching sessions found in the scanned records."
        if "next_server_offset" in page:
            out["message"] += " More records remain; continue with next_server_offset."
    return out


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


# Spec ``ServerConfigurationResult``.
_CLUSTER_FIELDS = (
    "name", "server_ip", "fqdn", "server_uuid", "is_publisher", "is_insight_enabled", "replication_status",
    "last_replication_timestamp",
)  # fmt: skip
_ENDPOINT_STATUSES = ("Known", "Unknown", "Disabled")


async def _part(coro: Any) -> Any:
    from casper_network_mcp.products._tools import _HANDLED, _as_error

    try:
        return await coro
    except _HANDLED as exc:
        return _as_error("clearpass", exc)


def _failed(part: Any) -> bool:
    return isinstance(part, dict) and "error" in part


def _count(data: Any) -> Any:
    return data.get("count") if isinstance(data, dict) else None


@mcp.tool()
async def clearpass_overview(top_failures: int = 5) -> dict[str, Any]:
    """How ClearPass is doing, in one call: version, cluster nodes, endpoint counts and recent login failures.

    Reads ``GET /api/server/version``, ``/api/cluster/server``,
    ``/api/endpoint`` (counts only, in total and per status: Known, Unknown,
    Disabled) and ``/api/session`` (failed authentications, newest first,
    with their count). A part that fails carries its own ``error`` and
    ``degraded`` is true.
    """
    top = clamp_limit(top_failures, default=5)

    def endpoint_count(status: str | None) -> Any:
        params: dict[str, Any] = {"limit": 1, "offset": 0, "calculate_count": "true", "profile_details": "false"}
        if status:
            params["filter"] = json.dumps({"status": status}, separators=(",", ":"))
        return _part(_get("/api/endpoint", params))

    failures_params = {
        "limit": top,
        "offset": 0,
        "calculate_count": "true",
        "filter": json.dumps({"auth_status": "FAILED"}, separators=(",", ":")),
        "sort": "-acctstarttime",
    }
    version, cluster, failures, total, *per_status = await asyncio.gather(
        _part(_get("/api/server/version")),
        _part(_get("/api/cluster/server")),
        _part(_get("/api/session", failures_params)),
        endpoint_count(None),
        *(endpoint_count(status) for status in _ENDPOINT_STATUSES),
    )
    out: dict[str, Any] = {"version": version}
    if _failed(cluster):
        out["cluster"] = cluster
    else:
        rows = _items(cluster) or ([cluster] if isinstance(cluster, dict) and cluster.get("name") else [])
        servers = [_pick(item, _CLUSTER_FIELDS) for item in rows]
        out["cluster"] = {"total": len(servers), "servers": servers[:25]}
    if _failed(total):
        out["endpoints"] = total
    else:
        endpoints: dict[str, Any] = {"total": _count(total)}
        for status, reply in zip(_ENDPOINT_STATUSES, per_status, strict=True):
            endpoints[status] = reply if _failed(reply) else _count(reply)
        out["endpoints"] = endpoints
    if _failed(failures):
        out["auth_failures"] = failures
    else:
        out["auth_failures"] = {
            "total": _count(failures),
            "recent": [_session(item) for item in _items(failures)[:top]],
        }
    out["degraded"] = any(_failed(p) for p in (version, cluster, failures, total, *per_status))
    return out


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


async def _role_id(role_name: str) -> int:
    """The id of the role named exactly ``role_name`` (``GET /api/role`` with a name filter)."""
    params = {"filter": json.dumps({"name": role_name}, separators=(",", ":")), "limit": 25, "offset": 0}
    found = [
        row["id"]
        for row in _items(await _get("/api/role", params))
        if isinstance(row, dict) and row.get("name") == role_name and isinstance(row.get("id"), int)
    ]
    if not found:
        raise ValueError(f"No ClearPass role is named {role_name!r}; see clearpass_list_roles, or pass role_id.")
    if len(found) > 1:
        raise ValueError(f"More than one ClearPass role is named {role_name!r} (ids {found}); pass role_id.")
    return found[0]


@mcp.tool()
async def clearpass_create_guest(
    username: str,
    password: str,
    role_id: Annotated[int | None, Field(description="Numeric id of the guest's role.")] = None,
    role_name: Annotated[
        str | None, Field(description="Exact role name, looked up to its id (GET /api/role) instead of role_id.")
    ] = None,
    dry_run: bool = False,
) -> Any:
    """Create a guest account (``POST /api/guest``) with a username, password and role.

    Give exactly one of ``role_id`` or ``role_name``. The password is hidden
    in previews and replies.
    """
    if (role_id is None) == (role_name is None):
        raise ValueError("Provide exactly one of role_id or role_name.")
    if role_id is None:
        role_id = await _role_id(role_name or "")
    body: dict[str, Any] = {"username": username, "password": password, "role_id": role_id}
    return await _change("POST", "/api/guest", body=body, dry_run=dry_run)


@mcp.tool()
async def clearpass_disconnect_session(session_id: str, dry_run: bool = False) -> Any:
    """Disconnect a live session with a change of authorisation (``POST /api/session/{id}/disconnect``)."""
    body = {"id": session_id, "confirm_disconnect": True}
    return await _change("POST", f"/api/session/{_seg(session_id)}/disconnect", body=body, dry_run=dry_run)


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
