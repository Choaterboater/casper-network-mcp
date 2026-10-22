"""Compact, GET-only WLAN discovery diagnostics; no client inventory or secrets."""

from __future__ import annotations

import asyncio
from typing import Any

from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.products.mist._common import MIST, extract_items, get, pick, seg

# Auto-discovery is for small scopes, not an unbounded org-wide crawl.
_MAX_SITES = 10
_SETTINGS = (
    "enabled", "vlan_enabled", "vlan_id", "vlan_pooling", "vlan_ids",
    "isolation", "l2_isolation", "limit_bcast", "allow_mdns", "allow_ssdp",
)  # fmt: skip


def _complete_items(data: Any) -> list[dict[str, Any]] | None:
    """Never turn a bounded, malformed or failed reply into 'not found'."""
    if isinstance(data, dict):
        if data.get("error") or data.get("truncated"):
            return None
        for key in ("_pagination", "_response_bounds"):
            bounds = data.get(key)
            if isinstance(bounds, dict) and bounds.get("truncated"):
                return None
        if not any(isinstance(data.get(key), list) for key in ("items", "results", "data")):
            return None
    elif not isinstance(data, list):
        return None
    items = extract_items(data)
    if isinstance(data, dict):
        total = data.get("_pagination", {}).get("total") if isinstance(data.get("_pagination"), dict) else None
        if isinstance(total, int) and total > len(items):
            return None
    return items if all(isinstance(item, dict) for item in items) else None


def _summary(wlan: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
    bonjour = wlan.get("bonjour")
    services = bonjour.get("services") if isinstance(bonjour, dict) else None
    findings = []
    if wlan.get("enabled") is False:
        findings.append("wlan_disabled")
    if wlan.get("isolation") is True or wlan.get("l2_isolation") is True:
        findings.append("client_isolation_enabled")
    if wlan.get("limit_bcast") is True and wlan.get("allow_mdns") is False:
        findings.append("mdns_filtered_check_bonjour_services" if services else "mdns_filtered")
    return {
        "site_id": site["id"],
        "site_name": site.get("name"),
        **pick(wlan, ("id", "ssid", "template_id", "for_site")),
        "settings": pick(wlan, _SETTINGS),
        "dynamic_vlan_enabled": (
            wlan["dynamic_vlan"].get("enabled") if isinstance(wlan.get("dynamic_vlan"), dict) else None
        ),
        "bonjour_services": sorted(services)[:20] if isinstance(services, dict) else None,
        "bonjour_service_count": len(services) if isinstance(services, dict) else None,
        "bonjour_additional_vlan_ids": bonjour.get("additional_vlan_ids") if isinstance(bonjour, dict) else None,
        "findings": findings,
    }


@MIST.tool()
async def mist_wlan_discovery_summary(
    ssid: str,
    site_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Check AirPrint / Bonjour / mDNS printer discovery for an SSID, with compact evidence.

    Start here for an iPhone that cannot find a printer. Supply site_id or
    org_id when known; otherwise resolve the login's scope through /self.
    Reads derived WLANs (including templates) with site variables resolved.
    Auto-discovery checks at most 10 sites; larger or ambiguous scopes need
    an explicit site_id. Returns all exact SSID matches, never chooses one
    for a change. No client inventory, site settings, writes or secrets.
    Findings are configuration clues, not proof of printer reachability.
    """
    if not ssid.strip():
        raise ValueError("ssid must not be empty")
    if site_id is not None and org_id is not None:
        raise ValueError("Supply site_id or org_id, not both")
    # Validate user-supplied path segments before any scope discovery traffic.
    if site_id is not None:
        seg(site_id)
    if org_id is not None:
        seg(org_id)
    sites: list[dict[str, Any]] = []
    if site_id is not None:
        sites = [{"id": site_id}]
    elif org_id is None:
        from casper_network_mcp.products.mist.tools import mist_whoami

        scope = await mist_whoami()
        orgs = scope["orgs"]
        sites = scope["sites"]
        if len(orgs) == 1 and all(site.get("org_id") == orgs[0]["id"] for site in sites):
            org_id = orgs[0]["id"]
            sites = []
        elif orgs or not sites:
            return {
                "status": "scope_required",
                "orgs": orgs[:_MAX_SITES],
                "org_count": len(orgs),
                "sites": sites[:_MAX_SITES],
                "site_count": len(sites),
                "next_action": "Supply an explicit org_id or site_id.",
            }
    if org_id is not None:
        data = await get(f"/api/v1/orgs/{seg(org_id)}/sites", {"limit": _MAX_SITES + 1, "page": 1}, org_id=org_id)
        items = _complete_items(data)
        if items is None or any(not isinstance(site.get("id"), str) for site in items):
            return {"status": "incomplete", "next_action": "Site list was incomplete; supply a site_id."}
        sites = [pick(site, ("id", "name")) for site in items]
    if len(sites) > _MAX_SITES:
        return {
            "status": "scope_required",
            "sites": sites[:_MAX_SITES],
            "next_action": "More than 10 sites are visible; supply a site_id. No WLANs scanned.",
        }

    semaphore = asyncio.Semaphore(4)

    async def inspect_site(site: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                data = await get(
                    f"/api/v1/sites/{seg(site['id'])}/wlans/derived", {"resolve": "true"}, site_id=site["id"]
                )
            except ToolError as exc:
                return {"site_id": site["id"], "failure": exc.as_error()}
            wlans = _complete_items(data)
            if wlans is None or any(not isinstance(wlan.get("ssid"), str) for wlan in wlans):
                return {"site_id": site["id"], "failure": {"error": "Derived WLAN list was incomplete."}}
            return {"matches": [_summary(wlan, site) for wlan in wlans if wlan["ssid"] == ssid]}

    results = await asyncio.gather(*(inspect_site(site) for site in sites))
    matches = [match for result in results for match in result.get("matches", [])]
    failures = [result for result in results if "failure" in result]
    status = (
        "incomplete" if failures else "multiple_matches" if len(matches) > 1 else "matched" if matches else "not_found"
    )
    return {
        "ssid": ssid,
        "status": status,
        "sites_checked": len(sites),
        "matches": matches,
        "failures": failures,
        "interpretation": "mdns_filtered can block AirPrint discovery; check Bonjour services if configured.",
        "next_action": (
            "Resolve scope/failures before selecting a WLAN."
            if failures or len(matches) > 1
            else "Check the SSID or supply its site_id."
            if not matches
            else "If mdns_filtered and printer/phone share a VLAN, allow mDNS while keeping multicast filtering on. "
            "Otherwise check printer advertisements, Bonjour gateway, routing and firewall access."
        ),
        "unverified": "Printer identity/VLAN, advertisements, AP deployment and end-to-end printing.",
    }
