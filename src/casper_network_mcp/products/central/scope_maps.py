"""Central scope maps, VLAN interfaces and the standard Aruba device profiles.

Lifted from hpe-networking-mcp pipeline/stages/s6_configure.py
(_fetch_global_scope_id, _post_scope_map, _push_vlan_interface,
_ensure_device_profiles, ARUBA_DEVICE_PROFILES) and the device-group
lookup they use from s2_validate.py (MIT, nowireless4u/hpe-networking-mcp).
What changed: no Stage, state store, models or account context; the
switch group name is an argument (it was an env read, default "Switches");
central_client is the gated Central shim (compat.get_client()), whose
errors carry .response.text like the source's httpx errors.
"""

from __future__ import annotations

import logging
from typing import Any

from casper_network_mcp.core.scope_ids import normalize_scope_id

__all__ = [
    "ARUBA_DEVICE_PROFILES",
    "CONFIG_ASSIGNMENTS",
    "DEFAULT_SWITCH_GROUP_NAME",
    "_ensure_device_profiles",
    "_fetch_global_scope_id",
    "_post_scope_map",
    "_push_vlan_interface",
    "assignment_body",
]

logger = logging.getLogger(__name__)

#: Where a profile is attached to a scope and device function. The source
#: posted to a ``scope-maps`` path that is in none of the bundled Central
#: documents; ``config-assignments`` is the documented way (Task 10).
CONFIG_ASSIGNMENTS = "/network-config/v1alpha1/config-assignments"


def assignment_body(scope_id: Any, device_function: str, resource: str) -> dict[str, Any]:
    """The config-assignments body for one ``<profile-type>/<instance>`` at one scope."""
    profile_type, _, instance = str(resource).partition("/")
    return {
        "config-assignment": [
            {
                "scope-id": str(scope_id),
                "device-function": device_function,
                "profile-type": profile_type,
                "profile-instance": instance,
            }
        ]
    }


def _is_idempotent_conflict(exc: Exception) -> bool:
    """True when a failed write is an idempotent no-op (resource already applied).

    Matches the "duplicate"/"already exists" markers Central returns on a
    resumed run so re-creating an existing scope-map/resource is non-fatal.
    """
    body = (getattr(getattr(exc, "response", None), "text", "") or "").lower()
    return "duplicate" in body or "already exists" in body


ARUBA_DEVICE_PROFILES: list[dict] = [
    {
        "name": "arubaAP",
        "role": "arubaAP",
        "lldp-group-entries": [
            {
                "sequence-number": 1,
                "action": "MATCH",
                "vendor-oui": "000B86",
                "vendor-oui-subtype": [{"type": 1, "value": "0001"}],
            },
        ],
    },
    {
        "name": "arubaGW",
        "role": "arubaGW",
        "lldp-group-entries": [
            {
                "sequence-number": 1,
                "action": "MATCH",
                "vendor-oui": "000B86",
                "vendor-oui-subtype": [{"type": 1, "value": "0002"}],
            },
        ],
    },
    {
        "name": "arubaSW",
        "role": "arubaSW",
        "lldp-group-entries": [
            {
                "sequence-number": 1,
                "action": "MATCH",
                "vendor-oui": "883A30",
                "vendor-oui-subtype": [{"type": 2, "value": "0001"}],
            },
            {
                "sequence-number": 2,
                "action": "MATCH",
                "vendor-oui": "0016B9",
                "vendor-oui-subtype": [{"type": 2, "value": "0001"}],
            },
        ],
    },
    {
        "name": "arubaAOS",
        "role": "arubaAOS",
        "lldp-group-entries": [
            {
                "sequence-number": 1,
                "action": "MATCH",
                "vendor-oui": "0016B9",
                "vendor-oui-subtype": [{"type": 2, "value": "0001"}],
            },
        ],
    },
]


def _fetch_global_scope_id(central_client: Any) -> str:
    """Return the account-root scope ID from Central's authoritative endpoint."""
    endpoint = "/network-config/v1/global"
    result = central_client.get(endpoint)
    if not isinstance(result, dict):
        raise RuntimeError(f"{endpoint} returned a non-object response")
    for key in ("scopeId", "id"):
        try:
            return normalize_scope_id(result.get(key), field_name=key)
        except ValueError:
            continue
    raise RuntimeError(f"{endpoint} response omitted a valid numeric scopeId")


# Library profiles are scope-mapped at the org root and at the switch device
# group. Both scope ids are worked out at run time from the tenant.
DEFAULT_SWITCH_GROUP_NAME = "Switches"

# GET /network-config/v1/device-groups (and its deprecated /v1alpha1/ sibling)
# both *require* limit and offset and cap limit at 100 per the committed
# Central manifest (getDeviceGroupsV1 / getDeviceGroups). A single unpaged
# limit=100 request therefore silently hid every group past the first page,
# which made validation reject perfectly valid target groups.
DEVICE_GROUP_PATHS = (
    "/network-config/v1/device-groups",
    "/network-config/v1alpha1/device-groups",
)
_GROUP_PAGE_SIZE = 100
_MAX_GROUP_PAGES = 200

# Response envelope keys seen for this collection, in priority order. A
# response carrying *none* of these is an unrecognized shape (e.g. an error
# envelope returned with a 200) and must trigger the fallback path rather
# than be read as "this account has no device groups".
_GROUP_COLLECTION_KEYS = ("items", "data", "device-groups", "deviceGroups", "groups")


class DeviceGroupLookupError(RuntimeError):
    """Raised when device groups could not be listed from any known path."""


def _extract_group_items(payload: Any) -> list[dict[str, Any]] | None:
    """Return the group list from a response, or ``None`` if unrecognized.

    Distinguishes "recognized envelope, zero groups" (``[]``) from "this is
    not a device-group collection response" (``None``) so only the latter
    falls through to the next API version.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return None
    for key in _GROUP_COLLECTION_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return None
    return None


def _group_name(group: dict[str, Any]) -> str:
    """Human-readable name of a device group across response shapes.

    Uses ``or`` chaining rather than ``dict.get(key, fallback)`` — the API
    returns explicit ``null`` for absent fields, and the old default-argument
    form propagated that ``None`` straight into the name set.
    """
    for key in ("scopeName", "scope_name", "group", "name", "groupName"):
        value = group.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fetch_device_groups(central_client: Any) -> list[dict[str, Any]]:
    """Return every device group in the account, paging until exhausted.

    Tries the current ``/v1/`` path first and falls back to the deprecated
    ``/v1alpha1/`` path only when ``/v1/`` errors or answers with a shape that
    is not a device-group collection. Raises :class:`DeviceGroupLookupError`
    when no path yields a usable response — callers must not treat an
    unreachable API as "the group does not exist".
    """
    failures: list[str] = []
    for path in DEVICE_GROUP_PATHS:
        groups: list[dict[str, Any]] = []
        offset = 0
        unrecognized = False
        errored: str | None = None
        for _ in range(_MAX_GROUP_PAGES):
            try:
                result = central_client.get(path, params={"limit": _GROUP_PAGE_SIZE, "offset": offset})
            except Exception as exc:
                errored = f"{path}: {exc}"
                break
            page = _extract_group_items(result)
            if page is None:
                unrecognized = True
                break
            groups.extend(page)
            if len(page) < _GROUP_PAGE_SIZE:
                break
            offset += _GROUP_PAGE_SIZE
        else:
            logger.warning(
                "Device-group paging stopped at the %d-page cap for %s — results may be incomplete.",
                _MAX_GROUP_PAGES,
                path,
            )
        if errored:
            failures.append(errored)
            continue
        if unrecognized:
            failures.append(f"{path}: unrecognized response shape")
            continue
        logger.debug("Fetched %d device group(s) from %s", len(groups), path)
        return groups
    raise DeviceGroupLookupError("could not list device groups from any known endpoint (" + "; ".join(failures) + ")")


def _resolve_device_group_scope_id(central_client: Any, group_name: str) -> str | None:
    """Return the scope-id of the named device group, or ``None`` if absent.

    Propagates lookup failures (:class:`DeviceGroupLookupError`) rather than
    swallowing them — an unreachable scope API must not be indistinguishable
    from "that group does not exist".
    """
    wanted = group_name.strip().lower()
    for group in _fetch_device_groups(central_client):
        if _group_name(group).lower() != wanted:
            continue
        for key in ("scopeId", "scope_id", "scope-id", "id"):
            value = group.get(key)
            if value not in (None, ""):
                return str(value)
        logger.warning(
            "Device group %r matched but carries no scope-id field (keys=%s)",
            group_name,
            sorted(group)[:20],
        )
        return None
    return None


def _post_scope_map(
    central_client: Any,
    scope_id: str,
    persona: str,
    resource: str,
) -> None:
    """POST one config assignment (scope, device function, ``<profile-type>/<name>``).

    Raises on HTTP error. Caller wraps in try/except.
    """
    scope_id = normalize_scope_id(scope_id)
    central_client.post(
        CONFIG_ASSIGNMENTS,
        data=assignment_body(scope_id, persona, resource),
    )


def _profile_scope_ids(central_client: Any, switch_group_name: str) -> list[str]:
    """Resolve the scope-ids library profiles are mapped into.

    Always includes the org-level global scope. Adds the switch device-group
    scope when it resolves. Raises RuntimeError if the global scope cannot
    be determined: every scope-map below would otherwise point at a guess.
    """
    global_scope_id = normalize_scope_id(_fetch_global_scope_id(central_client), field_name="global_scope_id")
    scope_ids = [global_scope_id]
    group_name = (switch_group_name or DEFAULT_SWITCH_GROUP_NAME).strip() or DEFAULT_SWITCH_GROUP_NAME
    group_scope_id = _resolve_device_group_scope_id(central_client, group_name)
    if group_scope_id:
        group_scope_id = normalize_scope_id(group_scope_id, field_name=f"device group {group_name!r} scope_id")
        if group_scope_id not in scope_ids:
            scope_ids.append(group_scope_id)
    else:
        logger.warning(
            "Device group %r not found: library profiles are mapped at the global scope only.",
            group_name,
        )
    return scope_ids


def _ensure_device_profiles(central_client: Any, switch_group_name: str = DEFAULT_SWITCH_GROUP_NAME) -> list[str]:
    """Create the four standard Aruba LLDP device profiles at the library level.

    Step 1: Create port profiles (roles); scope-map at the resolved global scope
            and the switch device-group scope.
    Step 2: Create sw-port-profiles; scope-map at the same scopes.
    Step 3: Create device profiles with LLDP match rules via v1alpha1.

    Scope IDs are resolved at runtime via ``_profile_scope_ids`` — a failure to
    resolve the global scope raises instead of proceeding against a guess.
    "Already exists" responses are skipped; any other write failure is
    collected and returned so the caller can surface it rather than losing it
    in a log line.

    Returns:
        Non-duplicate failure messages encountered while creating/mapping
        profiles (empty when everything succeeded or already existed).

    switch_group_name is the device group the profiles are also mapped
    into (default "Switches"); if no group has that name, only the global
    scope is used.
    """
    profile_errors: list[str] = []
    scope_ids = _profile_scope_ids(central_client, switch_group_name)
    _PROFILE_NAMES = [p["name"] for p in ARUBA_DEVICE_PROFILES]

    def _skip(response_text: str) -> bool:
        t = response_text.lower()
        return "duplicate" in t or "already exists" in t

    # Step 1: Port profiles (roles) — no policy needed for CX switch device identity roles
    _ROLE_BODIES: dict[str, dict] = {
        "arubaAP": {
            "name": "arubaAP",
            "description": "arubaAP",
            "vlan-parameters": {"wired-access-vlan-id": 5},
            "session-parameters": {
                "auth-mode": "DEVICE_MODE",
                "stp-admin-edge-port": True,
                "poe-priority": "CRITICAL",
                "tunneled-node-server-redirect": False,
            },
        },
    }
    for name in _PROFILE_NAMES:
        role_body = _ROLE_BODIES.get(name, {"name": name, "description": name})
        try:
            central_client.post(f"/network-config/v1alpha1/roles/{name}", data=role_body)
            logger.debug("Created port profile (role) '%s'", name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
            if _skip(resp_text):
                # Update existing role to ensure vlan-parameters/session-parameters are applied
                try:
                    central_client.put(f"/network-config/v1alpha1/roles/{name}", data=role_body)
                    logger.debug("Updated existing port profile (role) '%s'", name)
                except Exception as exc2:
                    profile_errors.append(f"role_update({name}): {exc2}")
                    logger.warning("Role '%s' update failed: %s — continuing", name, exc2)
            else:
                profile_errors.append(f"role_create({name}): {exc}")
                logger.warning("Port profile '%s' creation failed: %s — continuing", name, exc)

        # Scope-map role at the resolved global scope and switch device group
        for scope_id in scope_ids:
            try:
                _post_scope_map(central_client, scope_id, "ACCESS_SWITCH", f"roles/{name}")
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
                if not _skip(resp_text):
                    profile_errors.append(f"role_scope_map({name}@{scope_id}): {exc}")
                    logger.warning(
                        "Role '%s' scope-map (scope=%s) failed: %s — continuing",
                        name,
                        scope_id,
                        exc,
                    )

    # Step 2: Port profiles (sw-port-profiles)
    _PORT_PROFILES = [
        {
            "name": "aruba-ap-access",
            "description": "Aruba AP uplink — overlay mode, access VLAN 5",
            "body": {
                "mode": "AUTO",
                "enable": True,
                "routing": False,
                "dpi-enable": True,
                "lldp": {"mode": "TX_RX"},
                "switchport": {"interface-mode": "ACCESS", "access-vlan": 5},
                "stp": {
                    "enable": True,
                    "admin-edge-port": True,
                    "bpdu-guard": True,
                    "bpdu-filter": False,
                    "loop-guard": False,
                    "root-guard": False,
                    "rpvst-filter": False,
                    "rpvst-guard": False,
                    "tcn-guard": False,
                    "priority": 6,
                },
                "poe": {"enabled": True, "allocation-method": "USAGE", "priority": "CRITICAL"},
            },
        },
        {
            "name": "aruba-sw-trunk",
            "description": "Switch-to-switch trunk — all VLANs, no edge/guard, jumbo MTU",
            "body": {
                "mode": "AUTO",
                "enable": True,
                "routing": False,
                "dpi-enable": False,
                "mtu": 9198,
                "lldp": {"mode": "TX_RX"},
                "switchport": {
                    "interface-mode": "TRUNK",
                    "native-vlan": 1,
                },
                "stp": {
                    "enable": True,
                    "admin-edge-port": False,
                    "bpdu-guard": False,
                    "bpdu-filter": False,
                    "loop-guard": False,
                    "root-guard": False,
                    "rpvst-filter": False,
                    "rpvst-guard": False,
                    "tcn-guard": False,
                    "priority": 6,
                },
                "poe": {"enabled": False},
            },
        },
    ]
    for pp in _PORT_PROFILES:
        pp_name = pp["name"]
        try:
            central_client.post(
                f"/network-config/v1alpha1/sw-port-profiles/{pp_name}",
                data={"description": pp["description"]},
            )
            central_client.put(f"/network-config/v1alpha1/sw-port-profiles/{pp_name}", data=pp["body"])
            logger.debug("Created port profile '%s'", pp_name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
            if not _skip(resp_text):
                profile_errors.append(f"sw_port_profile_create({pp_name}): {exc}")
                logger.warning("Port profile '%s' creation failed: %s — continuing", pp_name, exc)

        for scope_id in scope_ids:
            try:
                _post_scope_map(central_client, scope_id, "ACCESS_SWITCH", f"sw-port-profiles/{pp_name}")
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
                if not _skip(resp_text):
                    profile_errors.append(f"sw_port_profile_scope_map({pp_name}@{scope_id}): {exc}")
                    logger.warning(
                        "Port profile '%s' scope-map (scope=%s) failed: %s — continuing",
                        pp_name,
                        scope_id,
                        exc,
                    )

    # Step 3: Device profiles with LLDP match rules + scope-maps
    for profile in ARUBA_DEVICE_PROFILES:
        name = profile["name"]
        try:
            central_client.post(f"/network-config/v1alpha1/device-profile/{name}", data=profile)
            logger.debug("Created device profile '%s'", name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
            if not _skip(resp_text):
                profile_errors.append(f"device_profile_create({name}): {exc}")
                logger.warning("Device profile '%s' creation failed: %s — continuing", name, exc)

        for scope_id in scope_ids:
            try:
                _post_scope_map(central_client, scope_id, "ACCESS_SWITCH", f"device-profile/{name}")
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
                if not _skip(resp_text):
                    profile_errors.append(f"device_profile_scope_map({name}@{scope_id}): {exc}")
                    logger.warning(
                        "Device profile '%s' scope-map (scope=%s) failed: %s — continuing",
                        name,
                        scope_id,
                        exc,
                    )

    return profile_errors


def _push_vlan_interface(
    central_client: Any,
    vi: dict,
    device_scope_id: str,
    global_scope_id: str,
    persona: str,
) -> None:
    """Push a single VLAN L3 interface.

    Args:
        vi: Dict with keys: vlan (int), ip_address (str|None),
            helper_address (str|None), dhcp (bool).
        device_scope_id: Numeric scope-id string for this device.
        global_scope_id: Org-level scope-id string.
        persona: e.g. "ACCESS_SWITCH".
    """
    vlan_id = vi["vlan"]

    # Step 1: Upsert L2 VLAN
    l2_body = {"vlan": vlan_id, "name": str(vlan_id), "enable": True}
    try:
        central_client.post(f"/network-config/v1alpha1/layer2-vlan/{vlan_id}", data=l2_body)
    except Exception as exc:
        response_text = getattr(getattr(exc, "response", None), "text", "") or ""
        if "duplicate" not in response_text.lower():
            central_client.put(f"/network-config/v1alpha1/layer2-vlan/{vlan_id}", data=l2_body)

    # Step 2: Create vlan-interface globally (no IP — just the L3 shell)
    global_body: dict = {"id": vlan_id, "is-valid": True, "enable": True}
    try:
        central_client.post(f"/network-config/v1alpha1/vlan-interfaces/{vlan_id}", data=global_body)
    except Exception as exc:
        response_text = getattr(getattr(exc, "response", None), "text", "") or ""
        if "duplicate" not in response_text.lower():
            central_client.put(f"/network-config/v1alpha1/vlan-interfaces/{vlan_id}", data=global_body)

    # Step 3: Override IP at device local scope
    if vi["ip_address"] and not vi["dhcp"]:
        local_body: dict = {"id": vlan_id, "is-valid": True, "enable": True, "ipv4": {"address": vi["ip_address"]}}
        if vi["helper_address"]:
            local_body["ipv4-relay"] = {
                "server": [
                    {
                        "ip": vi["helper_address"],
                        "vrf": "default",
                        "ip-vrf": f"{vi['helper_address']}~default",
                    }
                ]
            }
        local_params = {"scope-id": device_scope_id, "view-type": "LOCAL"}
        try:
            central_client.post(
                f"/network-config/v1alpha1/vlan-interfaces/{vlan_id}",
                params=local_params,
                data=local_body,
            )
        except Exception:
            central_client.put(
                f"/network-config/v1alpha1/vlan-interfaces/{vlan_id}",
                params=local_params,
                data=local_body,
            )

    # Step 4: Scope-maps — duplicates on a resumed run are non-fatal for BOTH
    # the global layer2-vlan map and the device vlan-interfaces map. The global
    # map used to be issued unguarded, so a re-run whose global scope-map
    # already existed aborted the whole VLAN push.
    for scope_id, resource in (
        (global_scope_id, f"layer2-vlan/{vlan_id}"),
        (device_scope_id, f"vlan-interfaces/{vlan_id}"),
    ):
        try:
            _post_scope_map(central_client, scope_id, persona, resource)
        except Exception as exc:
            if not _is_idempotent_conflict(exc):
                raise

    logger.debug(
        "Pushed VLAN interface %d (%s) for scope-id=%s",
        vlan_id,
        "dhcp" if vi["dhcp"] else vi["ip_address"],
        device_scope_id,
    )
