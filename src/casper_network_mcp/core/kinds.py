"""Change kinds: what sort of change each tool makes, using Casper's set exactly.

``read | troubleshoot | config | disruptive | firmware | delete | admin``.

Hand-written tools take their kind from their product's ``labels.yaml``
(checked by hand, one note per tool). Generated tools take it from
:func:`kind_for_operation`, first match wins:

0. ``KIND_OVERRIDES`` (hand-checked operations the word rules get wrong).
1. GET -> ``read``.
2. ``READ_POSTS`` -> ``read`` (POSTs that only read, each checked in its spec).
3. a firmware word -> ``firmware``.
4. DELETE, or a delete word -> ``delete``.
5. an account path segment or word -> ``admin``.
6. a disruptive word -> ``disruptive``.
7. ``TROUBLESHOOT_OPS`` -> ``troubleshoot``. A word never yields
   ``troubleshoot``: a write that only looks like a check falls to rule 8.
8. ``config``.

Words are whole words only: the path is split on ``/ _ - .`` and case changes
and the operationId on case changes and ``_``, so ``ping`` never matches
``mapping`` and ``show`` never matches ``slideshow``. A ``{parameter}``
name is not a word (``{image_id}`` does not make a call a firmware change).
The word lists contain Casper's own lists (``src/capabilities/kinds.ts``),
so a kind here is never less risky than Casper's reading of the same words.
"""

from __future__ import annotations

import functools
import re
from importlib.resources import files
from typing import Any, Literal, cast, get_args

import yaml  # type: ignore[import-untyped]
from mcp.types import ToolAnnotations

from casper_network_mcp.core.annotations import DESTRUCTIVE, DIAGNOSTIC, READ_ONLY, WRITE

__all__ = [
    "CHANGE_KINDS",
    "KIND_OVERRIDES",
    "READ_POSTS",
    "TROUBLESHOOT_OPS",
    "ChangeKind",
    "kind_for_operation",
    "label_for_kind",
    "labels",
    "listed_template",
    "path_words",
    "tool_labels",
]

ChangeKind = Literal["read", "troubleshoot", "config", "disruptive", "firmware", "delete", "admin"]
CHANGE_KINDS: tuple[str, ...] = get_args(ChangeKind)
#: Off by default in Casper until the person allows them.
RISKY_KINDS = frozenset({"firmware", "delete", "admin"})

_LABELS: dict[str, ToolAnnotations] = {
    "read": READ_ONLY,
    "troubleshoot": DIAGNOSTIC,
    "config": WRITE,
    "admin": WRITE,
    "firmware": WRITE,
    "disruptive": DESTRUCTIVE,
    "delete": DESTRUCTIVE,
}


def label_for_kind(kind: str) -> ToolAnnotations:
    """The MCP annotation a tool of ``kind`` carries."""
    try:
        return _LABELS[kind]
    except KeyError:
        raise ValueError(f"unknown change kind {kind!r}; use one of {', '.join(CHANGE_KINDS)}") from None


def tool_meta(kind: str) -> dict[str, Any]:
    """The ``_meta`` every tool carries so Casper sees its change kind."""
    if kind not in CHANGE_KINDS:
        raise ValueError(f"unknown change kind {kind!r}")
    return {"casper/change-kind": kind}


# ── Word lists (Casper's lists, plus the plan's) ────────────────────────────

FIRMWARE_WORDS = frozenset({"firmware", "upgrade", "downgrade", "image", "ota"})
DELETE_WORDS = frozenset({"delete", "remove", "unclaim", "erase", "zeroize", "destroy", "wipe", "purge", "factory"})
ADMIN_SEGMENTS = frozenset({"admins", "users", "roles", "sso", "apitokens", "invites", "privileges", "oauth"})
ADMIN_WORDS = frozenset(
    {
        "admin",
        "administrator",
        "sso",
        "saml",
        "invite",
        "invites",
        "token",
        "tokens",
        "apitoken",
        "apitokens",
        "rbac",
        "privilege",
        "privileges",
        "scim",
        "password",
        "credential",
        "credentials",
    }
)
#: Weaker account words, checked after the disruptive ones (disconnect_user stays disruptive).
ACCOUNT_WORDS = frozenset({"user", "users", "account", "accounts"})
DISRUPTIVE_WORDS = frozenset(
    {
        "bounce",
        "reboot",
        "restart",
        "reload",
        "disconnect",
        "deauth",
        "deauthenticate",
        "halt",
        "shutdown",
        "powercycle",
        "power",
        "reset",
        "kick",
        "cycle",
    }
)

_PLACEHOLDER = re.compile(r"\{[^}]*\}")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> list[str]:
    text = _ACRONYM.sub(r"\1 \2", _CAMEL.sub(r"\1 \2", text)).lower()
    return [w for w in _SPLIT.split(text) if w]


def path_words(path: str) -> list[str]:
    """The whole words in an API path, leaving out ``{parameter}`` names."""
    return _words(_PLACEHOLDER.sub("/", path))


def _segments(path: str) -> list[str]:
    return [s.lower() for s in _PLACEHOLDER.sub("", path).split("/") if s]


# ── Hand-checked lists ──────────────────────────────────────────────────────
# Each entry names an operation in the bundled specs (ClearPass paths carry the
# /api base they are sent under) and why it is listed. tests/test_kinds.py checks
# every key exists in a bundled spec.

READ_POST_REASONS: dict[tuple[str, str], str] = {
    (
        "POST",
        "/network-reporting/v1/reports/{report-id}/report-runs/{report-run-id}/download-link",
    ): "Central spec: 'Get Download Report Link' returns a download URL for a report run that already exists.",
    (
        "POST",
        "/network-config/v1alpha1/cnac-static-tags/usage",
    ): "Central spec: 'Get static tags usage information'; the body is the query, nothing is stored.",
}
READ_POSTS: frozenset[tuple[str, str]] = frozenset(READ_POST_REASONS)

_CENTRAL_TS = "/network-troubleshooting/v1/{device}/{{serial-number}}/{action}"
_CENTRAL_CHECKS: dict[str, tuple[str, ...]] = {
    "aps": ("ping", "traceroute", "showCommands", "getArpTable", "nslookup", "http", "https", "tcp"),
    "cx": ("ping", "traceroute", "showCommands", "http"),
    "aos-s": ("ping", "traceroute", "showCommands", "getArpTable"),
    "gateways": ("ping", "traceroute", "showCommands", "getArpTable", "http", "https"),
}
_CENTRAL_WHY = {
    "ping": "sends ICMP echo from the device and reports the replies",
    "traceroute": "traces the route from the device and reports the hops",
    "showCommands": "runs 'show' commands only; the spec rejects any command not starting with 'show '",
    "getArpTable": "reads the device's ARP table",
    "nslookup": "resolves a name from the device",
    "http": "makes one HTTP request from the device and reports the result",
    "https": "makes one HTTPS request from the device and reports the result",
    "tcp": "opens one TCP connection from the device and reports the result",
}
_MIST_DEVICE = "/api/v1/sites/{{site_id}}/devices/{{device_id}}/{action}"
_MIST_CHECKS: dict[str, str] = {
    "ping": "pings from the device; output streams to the websocket",
    "traceroute": "traceroute from the device; output streams to the websocket",
    "arp": "ARP lookup from the device; output streams to the websocket",
    "show_arp": "reads the ARP table",
    "show_bgp_summary": "reads the BGP summary",
    "show_dhcp_leases": "reads the DHCP leases",
    "show_dot1x": "reads the 802.1X table",
    "show_evpn_database": "reads the EVPN database",
    "show_forwarding_table": "reads the forwarding table",
    "show_mac_table": "reads the MAC table",
    "show_ospf_database": "reads the OSPF database",
    "show_ospf_interfaces": "reads the OSPF interfaces",
    "show_ospf_neighbors": "reads the OSPF neighbours",
    "show_ospf_summary": "reads the OSPF summary",
    "show_route": "reads the routing table",
    "show_service_path": "reads the SSR service path",
    "show_session": "reads the active sessions",
    "resolve_dns": "resolves names from the device",
    "service_ping": "pings a service from the SSR",
}

TROUBLESHOOT_REASONS: dict[tuple[str, str], str] = {}
for _device, _actions in _CENTRAL_CHECKS.items():
    for _action in _actions:
        TROUBLESHOOT_REASONS[("POST", _CENTRAL_TS.format(device=_device, action=_action))] = (
            f"Central spec ({_device}): {_CENTRAL_WHY[_action]}; changes nothing on the device."
        )
for _action, _why in _MIST_CHECKS.items():
    TROUBLESHOOT_REASONS[("POST", _MIST_DEVICE.format(action=_action))] = (
        f"Mist spec: {_why}; changes nothing on the device."
    )
TROUBLESHOOT_OPS: frozenset[tuple[str, str]] = frozenset(TROUBLESHOOT_REASONS)

_TDR = "a cable (TDR) test takes the tested port's link down while it runs"
_COA = "a change of authorisation can make the client re-authenticate or drop"
KIND_OVERRIDES: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/api/v1/installer/sites/{site_name}/optimize"): (
        "config",
        "a GET that starts RF optimisation for the site; the source server also treated it as a write",
    ),
    ("POST", "/api/v1/sites/{site_id}/rrm/optimize"): ("disruptive", "RRM optimisation moves AP channels and power"),
    ("POST", "/network-services/v1/airmatch-runnow"): (
        "disruptive",
        "AirMatch computes and deploys a new channel and power plan",
    ),
    ("POST", "/network-services/v1/ap-ranging-scans"): (
        "disruptive",
        "the spec says USE WITH CAUTION: APs leave their channel to range",
    ),
    ("POST", "/network-troubleshooting/v1/cx/{serial-number}/cableTest"): ("disruptive", _TDR),
    ("POST", "/network-troubleshooting/v1/aos-s/{serial-number}/cableTest"): ("disruptive", _TDR),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/cable_test"): ("disruptive", _TDR),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/clear_bgp"): ("disruptive", "resets the BGP sessions"),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/clear_session"): ("disruptive", "drops live sessions"),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/clear_dot1x"): (
        "disruptive",
        "ends 802.1X sessions so clients must re-authenticate",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/clear_mac_table"): (
        "disruptive",
        "flushes learned MACs; traffic floods until relearned",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/clear_macs"): (
        "disruptive",
        "flushes the MACs learned on a port; traffic floods until relearned",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/clear_arp"): (
        "disruptive",
        "flushes the ARP cache; traffic waits for ARP again",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/release_dhcp"): (
        "disruptive",
        "releases a DHCP lease; the address goes away",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/release_dhcp_leases"): (
        "disruptive",
        "releases DHCP leases; clients lose their addresses",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/vc/switch_master"): (
        "disruptive",
        "fails over the virtual chassis routing engines",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/restore_backup_version"): (
        "firmware",
        "rolls the device back to its backup software image",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/restore_backup_version"): (
        "firmware",
        "rolls devices back to their backup software images",
    ),
    ("PUT", "/api/v1/orgs/{org_id}/inventory"): (
        "delete",
        "op in the body can delete or unclaim devices (op=delete releases them from the org; downgrade_to_jsi)",
    ),
    ("PUT", "/api/v1/sites/{site_id}/devices/{device_id}/vc"): (
        "disruptive",
        "op in the body can remove or renumber a virtual chassis member",
    ),
    ("PUT", "/api/v1/installer/orgs/{org_id}/devices/{fpc0_mac}/vc"): (
        "disruptive",
        "op in the body can remove or renumber a virtual chassis member",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/vc/vc_port"): (
        "disruptive",
        "op=delete removes a virtual chassis port, which can split the virtual chassis",
    ),
    ("POST", "/api/onguard-activity/notification"): (
        "disruptive",
        "action Bounce makes the OnGuard agents bounce the endpoints' network connection now",
    ),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/shell"): (
        "admin",
        "opens a remote shell on the device: full command-line access",
    ),
    ("POST", "/api/v1/orgs/{org_id}/jsi/devices/{device_mac}/shell"): (
        "admin",
        "opens a remote shell on the device: full command-line access",
    ),
    ("POST", "/api/certificate/{cert_id}/reject"): ("delete", "rejects a signing request; it cannot be undone"),
    ("POST", "/api/certificate/{cert_id}/revoke"): ("delete", "revokes a certificate; it cannot be undone"),
    ("POST", "/api/extension/instance/{id}/stop"): ("disruptive", "stops a running ClearPass extension"),
    ("PATCH", "/api/server/service/{server_uuid}/{service_name}/stop"): (
        "disruptive",
        "stops a ClearPass service (RADIUS, policy and so on)",
    ),
    ("POST", "/api/active-session/{mac_address}"): ("disruptive", _COA),
    ("POST", "/api/session-action/coa"): ("disruptive", _COA),
    ("POST", "/api/session-action/coa/ip/{client_ip_address}"): ("disruptive", _COA),
    ("POST", "/api/session-action/coa/mac/{mac_address}"): ("disruptive", _COA),
    ("POST", "/api/session-action/coa/username/{username}"): ("disruptive", _COA),
    ("POST", "/api/session/{id}/reauthorize"): ("disruptive", _COA),
}


# ── Matching a path to a listed operation ──────────────────────────────────


def _template_regex(template: str) -> re.Pattern[str]:
    parts = _PLACEHOLDER.split(template)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


@functools.cache
def _templates() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    by_method: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for method, template in {*READ_POSTS, *TROUBLESHOOT_OPS, *KIND_OVERRIDES}:
        by_method.setdefault(method, []).append((template, _template_regex(template)))
    for entries in by_method.values():
        entries.sort()
    return by_method


def listed_template(method: str, path: str) -> str | None:
    """The listed template ``path`` is, or fills in (READ_POSTS, TROUBLESHOOT_OPS, KIND_OVERRIDES)."""
    method = method.upper()
    for template, regex in _templates().get(method, ()):
        if path == template or regex.match(path):
            return template
    return None


def kind_for_operation(method: str, path: str, operation_id: str = "") -> ChangeKind:
    """The change kind for one operation (a template or a concrete request path)."""
    method = method.upper()
    template = listed_template(method, path)
    key = (method, template) if template else None
    if key in KIND_OVERRIDES:
        return cast(ChangeKind, KIND_OVERRIDES[key][0])  # type: ignore[index]
    if method == "GET":
        return "read"
    if key in READ_POSTS:
        return "read"
    words = set(path_words(path)) | set(_words(operation_id))
    if words & FIRMWARE_WORDS:
        return "firmware"
    if method == "DELETE" or words & DELETE_WORDS:
        return "delete"
    if set(_segments(path)) & ADMIN_SEGMENTS or words & ADMIN_WORDS or ({"role", "assignment"} <= words):
        return "admin"
    if words & DISRUPTIVE_WORDS:
        return "disruptive"
    if words & ACCOUNT_WORDS:
        return "admin"
    if key in TROUBLESHOOT_OPS:
        return "troubleshoot"
    return "config"


# ── labels.yaml for hand-written tools ──────────────────────────────────────


@functools.cache
def labels(product: str) -> dict[str, dict[str, str]]:
    """``{tool_name: {kind, vendor_category, checked}}`` from ``products/<product>/labels.yaml``."""
    resource = files("casper_network_mcp") / "products" / product / "labels.yaml"
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{product} labels.yaml must be a mapping")
    for name, row in data.items():
        if not isinstance(row, dict) or row.get("kind") not in CHANGE_KINDS or not row.get("checked"):
            raise ValueError(f"{product} labels.yaml row {name!r} needs a known kind and a checked note")
    return data


def tool_labels(product: str, name: str) -> tuple[ToolAnnotations, dict[str, Any]]:
    """``(annotations, meta)`` for a hand-written tool, from its ``labels.yaml`` row."""
    row = labels(product).get(name)
    if row is None:
        raise KeyError(f"{product} tool {name!r} has no labels.yaml row")
    return label_for_kind(row["kind"]), tool_meta(row["kind"])
