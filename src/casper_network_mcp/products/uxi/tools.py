"""UXI curated tools, and the ``uxi`` backend that holds them.

Paths come from HPE's published UXI document (``api.capenetworks.com/docs/openapi.yaml``, 6.7.0),
all under ``/networking-uxi/v1alpha1``. Every request goes through the gated UXI client; every id
in a path goes through ``path_segment``. Change tools take ``dry_run: bool = False``; with
``dry_run=True`` they return ``{"would_send": {method, path, body}}`` and send nothing.

UXI's API gives configuration and status only: sensors, agents, groups, networks, service tests,
their group assignments, and each sensor's (or agent's) online / testing state and ongoing issues.
Test results (RSSI, latency, throughput) are not in it; UXI sends those through data push only.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.paths import path_segment
from casper_network_mcp.products._tools import ClientGetter, ToolSet, client, use_client, would_send
from casper_network_mcp.products.uxi.client import API

__all__ = ["backend", "backend_tools"]

mcp = ToolSet("uxi")

MERGE_PATCH = "application/merge-patch+json"
#: Assignment kind -> (path, the body field naming the thing assigned).
_ASSIGNMENTS = {
    "sensor": ("sensor-group-assignments", "sensorId"),
    "agent": ("agent-group-assignments", "agentId"),
    "network": ("network-group-assignments", "networkId"),
    "service_test": ("service-test-group-assignments", "serviceTestId"),
}
MAX_STATUS_CALLS = 50

Limit = Annotated[int, Field(description="Items per page, 1-100 (UXI's largest page).")]
Next = Annotated[str | None, Field(description="The next cursor from the previous page's reply.")]
AssignmentKind = Annotated[
    Literal["sensor", "agent", "network", "service_test"], Field(description="What is assigned to the group.")
]


def _ux() -> Any:
    return client("uxi")


def _seg(value: str) -> str:
    return path_segment(value)


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    clean = {k: v for k, v in (params or {}).items() if v is not None} or None
    return await _ux().request("GET", f"{API}{path}", params=clean, kind="read")


async def _list(path: str, limit: int, next: str | None) -> Any:
    return await _get(path, {"limit": max(1, min(int(limit), 100)), "next": next})


async def _change(
    method: str, path: str, *, body: Any = None, dry_run: bool = False, content_type: str = "application/json"
) -> Any:
    full = f"{API}{path}"
    if dry_run:
        return would_send(method, full, body)
    out = await _ux().request(method, full, json=body, content_type=content_type)
    return out if out is not None else {"ok": True}


def _fields(**values: Any) -> dict[str, Any]:
    body = {k: v for k, v in values.items() if v is not None}
    if not body:
        raise ValueError("Give at least one field to change.")
    return body


def _items(data: Any) -> list[dict[str, Any]]:
    rows = data.get("items") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


# ── reads ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def uxi_list_sensors(limit: Limit = 50, next: Next = None) -> Any:
    """UXI hardware sensors: id, name, serial, model, group, MACs, notes (``GET /sensors``)."""
    return await _list("/sensors", limit, next)


@mcp.tool()
async def uxi_list_agents(limit: Limit = 50, next: Next = None) -> Any:
    """UXI agents, UXI running as software on laptops, phones or VMs (``GET /agents``)."""
    return await _list("/agents", limit, next)


@mcp.tool()
async def uxi_get_status(sensor_id: str) -> Any:
    """One sensor's or agent's status: online, testing and its ongoing issues (``GET /sensors/{id}/status``).

    UXI reads an agent's status with the same call. A sensor counts as offline only after
    90 minutes without a report.
    """
    return await _get(f"/sensors/{_seg(sensor_id)}/status")


@mcp.tool()
async def uxi_status_overview(include_agents: bool = True, limit: int = MAX_STATUS_CALLS) -> dict[str, Any]:
    """Every sensor (and agent) with its online / testing state and ongoing issues, plus counts.

    One status call per sensor or agent, up to ``limit`` (at most 50); the rest are listed as not checked.
    """
    limit = max(1, min(int(limit), MAX_STATUS_CALLS))
    rows = [("sensor", r) for r in _items(await _list("/sensors", 100, None))]
    if include_agents:
        rows += [("agent", r) for r in _items(await _list("/agents", 100, None))]
    out: list[dict[str, Any]] = []
    for n, (kind, row) in enumerate(rows):
        entry = {"type": kind, "id": row.get("id"), "name": row.get("name"), "group": row.get("groupName")}
        if n >= limit or not row.get("id"):
            out.append({**entry, "checked": False})
            continue
        status = await _get(f"/sensors/{_seg(str(row['id']))}/status") or {}
        issues = [i for i in status.get("issues") or [] if isinstance(i, dict) and i.get("status") != "RESOLVED"]
        out.append({
            **entry,
            "online": status.get("isOnline"),
            "testing": status.get("isTesting"),
            "issues": [
                {k: i.get(k) for k in ("code", "severity", "timestamp")}
                | {"network": (i.get("context") or {}).get("networkName"),
                   "service_test": (i.get("context") or {}).get("serviceTestName")}
                for i in issues
            ],
        })  # fmt: skip
    checked = [r for r in out if r.get("checked", True)]
    return {
        "counts": {
            "listed": len(out),
            "checked": len(checked),
            "online": sum(1 for r in checked if r.get("online") is True),
            "offline": sum(1 for r in checked if r.get("online") is False),
            "with_issues": sum(1 for r in checked if r.get("issues")),
        },
        "items": out,
    }


@mcp.tool()
async def uxi_list_groups(limit: Limit = 50, next: Next = None) -> Any:
    """UXI groups (the hierarchy, often one per site): id, name, parent, path (``GET /groups``)."""
    return await _list("/groups", limit, next)


@mcp.tool()
async def uxi_list_service_tests(limit: Limit = 50, next: Next = None) -> Any:
    """UXI synthetic tests and their targets: name, category, template (``GET /service-tests``)."""
    return await _list("/service-tests", limit, next)


@mcp.tool()
async def uxi_list_wireless_networks(limit: Limit = 50, next: Next = None) -> Any:
    """Wireless networks UXI tests: name, SSID, security, band locking (``GET /wireless-networks``)."""
    return await _list("/wireless-networks", limit, next)


@mcp.tool()
async def uxi_list_wired_networks(limit: Limit = 50, next: Next = None) -> Any:
    """Wired networks UXI tests: name, VLAN, security (``GET /wired-networks``)."""
    return await _list("/wired-networks", limit, next)


@mcp.tool()
async def uxi_list_group_assignments(kind: AssignmentKind, limit: Limit = 50, next: Next = None) -> Any:
    """Which sensors, agents, networks or service tests are assigned to which group."""
    return await _list(f"/{_ASSIGNMENTS[kind][0]}", limit, next)


# ── changes ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def uxi_update_sensor(
    sensor_id: str,
    name: str | None = None,
    notes: str | None = None,
    address_note: str | None = None,
    pcap_mode: Literal["light", "full", "off"] | None = None,
    dry_run: bool = False,
) -> Any:
    """Rename a sensor or change its notes, address note or packet capture mode (``PATCH /sensors/{id}``)."""
    body = _fields(name=name, notes=notes, addressNote=address_note, pcapMode=pcap_mode)
    return await _change("PATCH", f"/sensors/{_seg(sensor_id)}", body=body, dry_run=dry_run, content_type=MERGE_PATCH)


@mcp.tool()
async def uxi_update_agent(
    agent_id: str,
    name: str | None = None,
    notes: str | None = None,
    pcap_mode: Literal["light", "full", "off"] | None = None,
    dry_run: bool = False,
) -> Any:
    """Rename an agent or change its notes or packet capture mode (``PATCH /agents/{id}``)."""
    body = _fields(name=name, notes=notes, pcapMode=pcap_mode)
    return await _change("PATCH", f"/agents/{_seg(agent_id)}", body=body, dry_run=dry_run, content_type=MERGE_PATCH)


@mcp.tool()
async def uxi_delete_agent(agent_id: str, dry_run: bool = False) -> Any:
    """Remove an agent from UXI (``DELETE /agents/{id}``)."""
    return await _change("DELETE", f"/agents/{_seg(agent_id)}", dry_run=dry_run)


@mcp.tool()
async def uxi_create_group(name: str, parent_id: str | None = None, dry_run: bool = False) -> Any:
    """Make a group under ``parent_id`` (the root group when left out) (``POST /groups``)."""
    body = {"name": name, **({"parentId": parent_id} if parent_id else {})}
    return await _change("POST", "/groups", body=body, dry_run=dry_run)


@mcp.tool()
async def uxi_rename_group(group_id: str, name: str, dry_run: bool = False) -> Any:
    """Rename a group (``PATCH /groups/{id}``)."""
    return await _change(
        "PATCH", f"/groups/{_seg(group_id)}", body={"name": name}, dry_run=dry_run, content_type=MERGE_PATCH
    )


@mcp.tool()
async def uxi_delete_group(group_id: str, dry_run: bool = False) -> Any:
    """Delete a group, its child groups and every assignment to them (``DELETE /groups/{id}``)."""
    return await _change("DELETE", f"/groups/{_seg(group_id)}", dry_run=dry_run)


@mcp.tool()
async def uxi_assign_to_group(kind: AssignmentKind, item_id: str, group_id: str, dry_run: bool = False) -> Any:
    """Assign a sensor, agent, network or service test to a group (``POST /<kind>-group-assignments``).

    A sensor tests the networks and service tests assigned to its group.
    """
    path, field = _ASSIGNMENTS[kind]
    return await _change("POST", f"/{path}", body={field: item_id, "groupId": group_id}, dry_run=dry_run)


@mcp.tool()
async def uxi_remove_group_assignment(kind: AssignmentKind, assignment_id: str, dry_run: bool = False) -> Any:
    """Remove one group assignment by its id (``DELETE /<kind>-group-assignments/{id}``)."""
    return await _change("DELETE", f"/{_ASSIGNMENTS[kind][0]}/{_seg(assignment_id)}", dry_run=dry_run)


# ── the backend ─────────────────────────────────────────────────────────────


def backend(client: ClientGetter | None = None) -> MCPServer:
    """The ``uxi`` backend: every hand-written UXI tool, labelled from ``labels.yaml``."""
    if client is not None:
        use_client("uxi", client)
    server = MCPServer("uxi")
    mcp.register(server)
    return server


def backend_tools() -> list[Any]:
    """The registered UXI tools (``.name``, ``.fn``, ``.parameters``)."""
    return list(sdk_compat.tool_registry(backend()).values())
