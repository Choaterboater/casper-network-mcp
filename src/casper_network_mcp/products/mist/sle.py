"""SLE (service level expectation) tools, as Mist's API defines them.

Adapted from mist-mcp ``mist_mcp/tools/sle.py`` (MIT). Site SLEs are per scope:
list the metrics a scope has, then read one metric's summary.

* ``GET /api/v1/sites/{site_id}/sle/{scope}/{scope_id}/metrics``
* ``GET /api/v1/sites/{site_id}/sle/{scope}/{scope_id}/metric/{metric}/summary``

``scope`` is ap, client, gateway, site or switch (the spec's enum); ``scope_id``
is the site id when scope is site, else the device id or client MAC.
Org-wide: ``GET /api/v1/orgs/{org_id}/insights/sites-sle``. Changed from the
source: ``org_id`` and ``site_id`` are required arguments (no default org
from the environment), and every id goes through ``path_segment``.
"""

from __future__ import annotations

from typing import Any, Literal

from casper_network_mcp.products.mist._common import MIST, get, seg, window

mcp = MIST

Scope = Literal["ap", "client", "gateway", "site", "switch"]


def _scope_path(site_id: str, scope: str, scope_id: str | None) -> str:
    if scope == "site":
        # The site is its own scope: its id fills both places.
        return f"/api/v1/sites/{seg(site_id)}/sle/site/{seg(site_id)}"
    if not scope_id:
        raise ValueError(f"scope_id is required when scope is {scope} (the device id or client MAC)")
    return f"/api/v1/sites/{seg(site_id)}/sle/{scope}/{seg(scope_id)}"


@mcp.tool()
async def sle_metrics(site_id: str, scope: Scope = "site", scope_id: str | None = None) -> Any:
    """The SLE metrics Mist tracks for a site, AP, switch, gateway or client.

    Use a returned metric name with ``sle_summary``. ``scope_id``: the device
    id (ap/switch/gateway) or client MAC; not needed for scope=site.
    """
    return await get(f"{_scope_path(site_id, scope, scope_id)}/metrics", site_id=site_id)


@mcp.tool()
async def sle_summary(
    site_id: str,
    metric: str,
    scope: Scope = "site",
    scope_id: str | None = None,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> Any:
    """One SLE metric's summary for a site, AP, switch, gateway or client.

    ``metric``: a name from ``sle_metrics`` (for example time-to-connect,
    coverage, throughput). ``duration``: "1h", "1d" (default), "7d"; or
    ``start``/``end`` as epoch seconds instead.
    """
    path = f"{_scope_path(site_id, scope, scope_id)}/metric/{seg(metric)}/summary"
    return await get(path, window(duration, start, end), site_id=site_id)


@mcp.tool()
async def sle_org_summary(
    org_id: str,
    sle: Literal["wifi", "wired", "wan"] | None = None,
    duration: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
) -> Any:
    """SLE insights for every site in the org (``GET /api/v1/orgs/{org_id}/insights/sites-sle``).

    ``sle``: wifi, wired or wan (default: all). ``duration``: "1d" (default),
    "7d"; or ``start``/``end`` epoch seconds.
    """
    params = window(duration, start, end)
    if sle:
        params["sle"] = sle
    if limit:
        params["limit"] = limit
    return await get(f"/api/v1/orgs/{seg(org_id)}/insights/sites-sle", params, org_id=org_id)
