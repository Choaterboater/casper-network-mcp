"""``access_check`` (casper/access-check v2) and the Mist scopes the gate enforces.

Adapted from Casper's ``docs/patches/hpe-networking-mcp-access-check.patch``
(Mist and ClearPass rules) and mist-mcp's ``orgs_list`` (privileges from
``/self``), extended to sites and site groups.

``access``, ``can_change`` and ``read_only`` always describe the login itself,
with or without ``--read-only``; the pin is reported separately in
``server_gate`` (``state: "off"`` means this server's writes are off).

* Mist: ``GET /api/v1/self`` privileges. Roles ``admin`` and ``write`` go to
  ``can_change``; ``read``, ``helpdesk`` and ``installer`` to ``read_only``
  (the role enum of ``admin_privilege`` in the bundled Mist spec). Any other
  role makes Mist ``unknown`` and lists nothing.
* ClearPass: ``GET /api/oauth/me`` and ``/api/oauth/privileges``; a privilege
  starting with ``#`` is read-only. ClearPass has no org or site scopes.
* Central: ``unknown`` with no lists and no call (Decision 6: no GreenLake in
  0.1.0, so there is no role answer to read).

A list is left out whole when any name or id is not plain
(``^[A-Za-z0-9 _.@:/+-]{1,64}$``), when it holds more than 64 scopes, or
when the login has a write privilege at a scope Casper can't show (msp,
orggroup), since a shorter list would understate where it can change things.
A product that answers 401 is reported with ``"login": "expired"`` so Casper
can ask for a new login. Tokens and raw payloads are never returned.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products._base import ApiError

__all__ = ["CONTRACT", "MistScopes", "access_check", "load_mist_scopes", "mist_scopes_from_self"]

CONTRACT = "casper/access-check v2"
PRODUCTS = ("central", "mist", "clearpass")
MAX_SCOPES = 64
_PLAIN = re.compile(r"^[A-Za-z0-9 _.@:/+-]{1,64}$")
_PER_PRODUCT_TIMEOUT_S = 10.0

MIST_WRITE_ROLES = frozenset({"admin", "write"})
MIST_READ_ROLES = frozenset({"read", "helpdesk", "installer"})


def _plain(value: Any) -> str | None:
    return value if isinstance(value, str) and _PLAIN.match(value) else None


@dataclass(frozen=True)
class Scope:
    kind: str  # org | site | sitegroup
    id: str
    name: str

    def label(self) -> str:
        return _plain(self.name) or self.id


def _listed(scopes: list[Scope]) -> list[dict[str, str]] | None:
    """The v2 list, or None when it must be left out (all or nothing)."""
    if not scopes or len(scopes) > MAX_SCOPES:
        return None
    out = []
    for scope in scopes:
        if not _plain(scope.id) or not _plain(scope.name):
            return None
        out.append({"kind": scope.kind, "id": scope.id, "name": scope.name})
    return out


def _names(scopes: list[Scope]) -> str:
    labels = [s.label() for s in scopes]
    if len(labels) > 3:
        return f"{', '.join(labels[:3])} and {len(labels) - 3} more"
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"


@dataclass
class MistScopes:
    """A Mist login's privileges, as access_check reports them and the gate enforces them."""

    access: str
    identity: str | None = None
    role: str | None = None
    can_change: list[Scope] = field(default_factory=list)
    read_only: list[Scope] = field(default_factory=list)
    can_change_complete: bool = True
    read_only_complete: bool = True
    _sites: dict[str, dict[str, Any]] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"product": "mist", "access": self.access}
        if _plain(self.identity):
            entry["identity"] = self.identity
        if _plain(self.role):
            entry["role"] = self.role
        if self.access == "unknown":
            return entry
        can_change = _listed(self.can_change) if self.can_change_complete else None
        read_only = _listed(self.read_only) if self.read_only_complete else None
        if can_change:
            entry["can_change"] = can_change
        if read_only:
            entry["read_only"] = read_only
        return entry

    # ── the gate's check ──────────────────────────────────────────────────

    def _ids(self, kind: str) -> set[str]:
        return {s.id for s in self.can_change if s.kind == kind}

    async def _site(self, client: Any, site_id: str) -> dict[str, Any] | None:
        """The site's org, site groups and name (cached); None when it can't be read."""
        if site_id in self._sites:
            return self._sites[site_id]
        try:
            data = await client.request("GET", f"/api/v1/sites/{site_id}", kind="read")
        except Exception:  # noqa: BLE001 - any failed lookup means "the product decides"
            return None
        if not isinstance(data, dict) or not isinstance(data.get("org_id"), str):
            return None
        groups = data.get("sitegroup_ids")
        info = {
            "org_id": data["org_id"],
            "sitegroup_ids": [g for g in groups if isinstance(g, str)] if isinstance(groups, list) else [],
            "name": data.get("name"),
        }
        self._sites[site_id] = info
        return info

    def _refuse(self, target: str) -> str:
        return f"This login can change {_names(self.can_change)} only; {target} is read-only. Nothing was sent."

    async def refusal(self, client: Any, method: str, path: str, args: dict[str, Any]) -> str | None:
        if self.access != "read-write" or not self.can_change_complete or not self.can_change:
            return None
        target = mist_target(path)
        if target is None:
            return None
        kind, target_id, org_id = target
        orgs, sites, groups = self._ids("org"), self._ids("site"), self._ids("sitegroup")
        if kind == "site":
            if target_id in sites:
                return None
            info = await self._site(client, target_id)
            if info is None:
                return None
            if info["org_id"] in orgs or groups & set(info["sitegroup_ids"]):
                return None
            return self._refuse(_plain(info.get("name")) or target_id)
        if kind == "sitegroup":
            if target_id in groups or org_id in orgs:
                return None
            return self._refuse(target_id)
        if target_id in orgs:
            return None
        return self._refuse(target_id)


def mist_target(path: str) -> tuple[str, str, str | None] | None:
    """``(kind, id, org_id)`` a Mist request path writes to, or None."""
    parts = [unquote(p) for p in path.split("?", 1)[0].split("/") if p]
    if len(parts) < 4 or parts[:2] != ["api", "v1"]:
        return None
    if parts[2] == "sites":
        return "site", parts[3], None
    if parts[2] == "orgs":
        if len(parts) >= 6 and parts[4] == "sitegroups":
            return "sitegroup", parts[5], parts[3]
        return "org", parts[3], parts[3]
    return None


def _privilege_scopes(privilege: dict[str, Any]) -> list[Scope] | None:
    """The org, site or site-group scopes one privilege names; None for one Casper can't show."""
    scope = privilege.get("scope")
    name = privilege.get("name")
    name = name if isinstance(name, str) else ""
    if scope == "org" and isinstance(privilege.get("org_id"), str):
        return [Scope("org", privilege["org_id"], name)]
    if scope == "site" and isinstance(privilege.get("site_id"), str):
        return [Scope("site", privilege["site_id"], name)]
    if scope == "sitegroup":
        ids = privilege.get("sitegroup_ids")
        if isinstance(privilege.get("sitegroup_id"), str):
            ids = [privilege["sitegroup_id"]]
        if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
            return [Scope("sitegroup", i, name) for i in ids]
    return None


def _add(scopes: list[Scope], new: list[Scope]) -> None:
    seen = {(s.kind, s.id) for s in scopes}
    for scope in new:
        if (scope.kind, scope.id) not in seen:
            scopes.append(scope)
            seen.add((scope.kind, scope.id))


def mist_scopes_from_self(payload: Any) -> MistScopes:
    """Read a ``GET /api/v1/self`` reply."""
    if not isinstance(payload, dict):
        return MistScopes("unknown")
    identity = payload.get("email") or payload.get("name")
    privileges = payload.get("privileges")
    if not isinstance(privileges, list) or not privileges or not all(isinstance(p, dict) for p in privileges):
        return MistScopes("unknown", identity=identity)
    roles = [p.get("role") for p in privileges]
    if not all(isinstance(r, str) and r in MIST_WRITE_ROLES | MIST_READ_ROLES for r in roles):
        return MistScopes("unknown", identity=identity)
    out = MistScopes(
        "read-write" if any(r in MIST_WRITE_ROLES for r in roles) else "read-only",
        identity=identity,
        role="/".join(sorted({str(r) for r in roles})),
    )
    for privilege in privileges:
        writes = privilege["role"] in MIST_WRITE_ROLES
        scopes = _privilege_scopes(privilege)
        if scopes is None:
            if writes:
                out.can_change_complete = False
            else:
                out.read_only_complete = False
            continue
        _add(out.can_change if writes else out.read_only, scopes)
    return out


async def load_mist_scopes(client: Any) -> MistScopes | None:
    """The gate's loader: the login's privileges from ``/self`` (None when unreadable)."""
    data = await client.request("GET", "/api/v1/self", kind="read")
    scopes = mist_scopes_from_self(data)
    return scopes if scopes.access != "unknown" else None


# ── access_check ─────────────────────────────────────────────────────────────


def clearpass_access_from_privileges(payload: Any) -> str:
    """Read-only when every privilege starts with ``#``."""
    privileges = payload.get("privileges") if isinstance(payload, dict) else None
    if not isinstance(privileges, list) or not privileges or not all(isinstance(p, str) for p in privileges):
        return "unknown"
    return "read-only" if all(p.startswith("#") for p in privileges) else "read-write"


async def _mist(client: Any, gate: Gate) -> dict[str, Any]:
    data = await client.request("GET", "/api/v1/self", kind="read")
    scopes = mist_scopes_from_self(data)
    if scopes.access != "unknown":
        gate.scopes["mist"] = scopes  # the same answer the gate enforces
    return scopes.report()


async def _clearpass(client: Any) -> dict[str, Any]:
    me = await client.request("GET", "/api/oauth/me", kind="read")
    privileges = await client.request("GET", "/api/oauth/privileges", kind="read")
    entry: dict[str, Any] = {"product": "clearpass", "access": clearpass_access_from_privileges(privileges)}
    identity = (me.get("info") or me.get("name")) if isinstance(me, dict) else None
    if _plain(identity):
        entry["identity"] = identity
    return entry


async def _product(product: str, client: Any, gate: Gate) -> dict[str, Any]:
    if not client.has_login:
        return {"product": product, "access": "unknown", "login": "missing"}
    if product == "central":
        return {"product": "central", "access": "unknown"}
    try:
        check = _mist(client, gate) if product == "mist" else _clearpass(client)
        return await asyncio.wait_for(check, timeout=_PER_PRODUCT_TIMEOUT_S)
    except ApiError as exc:
        if exc.login_expired:
            return {"product": product, "access": "unknown", "login": "expired"}
        return {"product": product, "access": "unknown"}
    except Exception:  # noqa: BLE001 - any failure is "unknown", never an error payload
        return {"product": product, "access": "unknown"}


async def access_check(clients: dict[str, Any], gate: Gate) -> dict[str, Any]:
    """The casper/access-check v2 answer for every product."""
    state = "off" if gate.read_only else "on"
    entries = await asyncio.gather(*(_product(p, clients[p], gate) for p in PRODUCTS))
    products = [{**entry, "server_gate": {"flag": "--read-only", "state": state}} for entry in entries]
    return {"contract": CONTRACT, "products": products}
