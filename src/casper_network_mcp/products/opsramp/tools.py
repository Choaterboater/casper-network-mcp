"""OpsRamp curated tools, and the ``opsramp`` backend that holds them.

Paths come from OpsRamp's public API reference (develop.opsramp.com, v2), all under
``/api/v2/tenants/<tenant>``. Every request goes through the gated OpsRamp client; every id in a
path goes through ``path_segment``. Change tools take ``dry_run: bool = False``; with
``dry_run=True`` they return ``{"would_send": {method, path, body}}`` and send nothing.

A ``queryString`` uses ``:`` between a field and its value, ``,`` between values and ``+`` between
fields (for example ``states:CRITICAL,WARNING+actions:OPEN``). The client sends spaces as ``%20``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.paths import path_segment
from casper_network_mcp.products._tools import ClientGetter, ToolSet, client, use_client, would_send

__all__ = ["backend", "backend_tools"]

mcp = ToolSet("opsramp")

OPEN_ALERTS = "states:CRITICAL,WARNING"
OPEN_INCIDENTS = "status:New,Open,Pending,On Hold"
ACTIVE_RESOURCES = "state:active"
#: Our action name -> the action in OpsRamp's path.
ALERT_ACTIONS = {
    "acknowledge": "acknowledge",
    "unacknowledge": "unAcknowledge",
    "suppress": "suppress",
    "unsuppress": "unSuppress",
    "close": "close",
    "heal": "heal",
}

Query = Annotated[
    str | None,
    Field(description="OpsRamp queryString: field:value, values joined by ',', fields joined by '+'."),
]
PageNo = Annotated[int, Field(description="Page number, from 1.")]
PageSize = Annotated[int, Field(description="Rows per page, 1-500.")]


def _op() -> Any:
    return client("opsramp")


def _seg(value: str) -> str:
    return path_segment(value)


async def _get(sub: str, params: dict[str, Any] | None = None) -> Any:
    c = _op()
    clean = {k: v for k, v in (params or {}).items() if v is not None} or None
    return await c.request("GET", f"{c.tenant_path}{sub}", params=clean, kind="read")


async def _search(
    sub: str, query: str | None, page_no: int, page_size: int, sort_name: str | None, descending: bool | None
) -> Any:
    params = {
        "queryString": query or None,
        "pageNo": max(1, int(page_no)),
        "pageSize": max(1, min(int(page_size), 500)),
        "sortName": sort_name,
        "isDescendingOrder": descending,
    }
    return await _get(sub, params)


def _total(data: Any) -> int | None:
    value = data.get("totalResults") if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ── reads ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def opsramp_overview() -> dict[str, Any]:
    """How many alerts are open (critical, warning), open incidents and active resources, in three searches."""
    critical = await _search("/alerts/search", "states:CRITICAL", 1, 1, None, None)
    warning = await _search("/alerts/search", "states:WARNING", 1, 1, None, None)
    out: dict[str, Any] = {"critical_alerts": _total(critical), "warning_alerts": _total(warning)}
    incidents = await _search("/incidents/search", OPEN_INCIDENTS, 1, 1, None, None)
    out["open_incidents"] = _total(incidents)
    resources = await _search("/resources/search", ACTIVE_RESOURCES, 1, 1, None, None)
    out["active_resources"] = _total(resources)
    return out


@mcp.tool()
async def opsramp_search_alerts(
    query: Query = OPEN_ALERTS,
    page_no: PageNo = 1,
    page_size: PageSize = 50,
    sort_name: Literal["id", "ts", "startaltime", "c_name", "p_name", "source", "rtype", "rname"] | None = None,
    descending: bool | None = None,
) -> Any:
    """Search OpsRamp alerts with a queryString (``GET /alerts/search``).

    The default query is CRITICAL or WARNING alerts. Other filters: ``actions:ACKNOWLEDGED``, ``alertTypes:Monitoring``, ``resourceIds:...``,
    ``startDate:2026-01-01T00:00:00 0000``.
    """
    return await _search("/alerts/search", query, page_no, page_size, sort_name, descending)


@mcp.tool()
async def opsramp_get_alert(alert_id: str) -> Any:
    """One alert's details: state, subject, description, resource, metric, priority, flags (``GET /alerts/{id}``)."""
    return await _get(f"/alerts/{_seg(alert_id)}")


@mcp.tool()
async def opsramp_search_incidents(
    query: Query = OPEN_INCIDENTS,
    page_no: PageNo = 1,
    page_size: PageSize = 50,
) -> Any:
    """Search incidents (``GET /incidents/search``); by default the open ones, created in the last 30 days."""
    return await _search("/incidents/search", query, page_no, page_size, None, None)


@mcp.tool()
async def opsramp_search_resources(
    query: Query = ACTIVE_RESOURCES,
    page_no: PageNo = 1,
    page_size: PageSize = 50,
) -> Any:
    """Search monitored resources (``GET /resources/search``); by default the active ones.

    Filters such as ``hostName:web-1``, ``resourceName:db``, ``ipAddress:192.0.2.10``, ``deviceGroup:...``.
    """
    return await _search("/resources/search", query, page_no, page_size, None, None)


@mcp.tool()
async def opsramp_get_resource(resource_id: str) -> Any:
    """One resource's details (``GET /resources/{id}``)."""
    return await _get(f"/resources/{_seg(resource_id)}")


# ── changes ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def opsramp_alert_action(
    alert_id: str,
    action: Literal["acknowledge", "unacknowledge", "suppress", "unsuppress", "close", "heal"],
    comment: str = "",
    snooze_minutes: Annotated[int | None, Field(description="With suppress: how long, in minutes.")] = None,
    dry_run: bool = False,
) -> Any:
    """Acknowledge, unacknowledge, suppress, unsuppress, close or heal one alert.

    ``POST /alerts/{id}/actions/{action}``; the comment goes in the alert's history.
    """
    body: dict[str, Any] = {}
    if comment.strip():
        body["description"] = comment.strip()
    if snooze_minutes is not None:
        if action != "suppress":
            raise ValueError("snooze_minutes goes with action suppress only.")
        body["snoozeDuration"] = max(1, int(snooze_minutes))
    c = _op()
    path = f"{c.tenant_path}/alerts/{_seg(alert_id)}/actions/{ALERT_ACTIONS[action]}"
    if dry_run:
        return would_send("POST", path, body)
    out = await c.request("POST", path, json=body)
    return out if out is not None else {"ok": True}


# ── the backend ─────────────────────────────────────────────────────────────


def backend(client: ClientGetter | None = None) -> MCPServer:
    """The ``opsramp`` backend: every hand-written OpsRamp tool, labelled from ``labels.yaml``."""
    if client is not None:
        use_client("opsramp", client)
    server = MCPServer("opsramp")
    mcp.register(server)
    return server


def backend_tools() -> list[Any]:
    """The registered OpsRamp tools (``.name``, ``.fn``, ``.parameters``)."""
    return list(sdk_compat.tool_registry(backend()).values())
