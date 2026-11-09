"""AOS 8 curated read tools, and the ``aos8`` backend that holds them.

Adapted from hpe-networking-mcp ``mcp_servers/aos8.py`` (its client, role and show tools). What changed:

* every request goes through the gated AOS 8 client (the read-only pin first, then the
  path check, then the login); every tool only sends ``GET``, so all of them are reads;
* show commands are built here from checked arguments: a MAC (any format), an IP, a plain
  user or profile name. ``aos8_show_command`` runs only ``show`` commands on an allow-list, one line,
  no ``;`` or ``|``, so no command reaches the running config or a secret;
* replies are cut to what a troubleshooting answer needs (``Users`` rows become ``mac, ip,
  user, role, vlan, ap, essid, auth, ...``), lists are bounded, and a value next to a key,
  secret or password is hidden in text output too;
* new: ``aos8_auth_trace`` (``show auth-tracebuf mac``), the controller's own record of the
  802.1X / MAC-auth / RADIUS steps for one client, and ``aos8_list_aaa_servers`` /
  ``aos8_list_aaa_profiles``, which say which RADIUS servers (often ClearPass) a profile uses;
* gone: logins as tools (the client logs in, reuses its session and logs out), the
  generated ``/object`` tools (``aos8_get_config_object`` reads a short list of auth and role
  objects), and every change tool.

AOS 8's showcommand API answers a table as ``{"<Title>": [rows], "_meta": [columns]}`` and
free text as ``{"_data": ["line", ...]}``; both are read here. A line starting with ``%`` is
the controller refusing the command.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.budget import bound_collection_response, clamp_limit
from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.redact import HIDDEN
from casper_network_mcp.products._tools import ClientGetter, ToolSet, client, use_client
from casper_network_mcp.products.aos8.client import OBJECT_PATH, SHOW_PATH

__all__ = ["ALLOWED_SHOW_PREFIXES", "CONFIG_OBJECTS", "backend", "backend_tools", "check_show_command"]

mcp = ToolSet("aos8")

MAX_TEXT_LINES = 200
MAX_OTHER_FIELDS = 30
MAX_VALUE_CHARS = 300

#: ``show`` commands ``aos8_show_command`` runs: client, AP, auth and AAA state. No config dumps, no secrets.
ALLOWED_SHOW_PREFIXES: tuple[str, ...] = (
    "show user-table",
    "show user",
    "show global-user-table",
    "show station-table",
    "show auth-tracebuf",
    "show ap database",
    "show ap active",
    "show ap details",
    "show ap association",
    "show ap client trail-info",
    "show ap bss-table",
    "show ap radio-summary",
    "show ap arm history",
    "show ap debug client-table",
    "show ap-group",
    "show wlan virtual-ap",
    "show aaa profile",
    "show aaa server-group",
    "show aaa authentication-server",
    "show aaa authentication dot1x",
    "show aaa authentication mac",
    "show aaa authentication captive-portal",
    "show aaa state",
    "show aaa derivation-rules",
    "show rights",
    "show netdestination",
    "show ip access-list",
    "show vlan",
    "show ip interface brief",
    "show datapath user",
    "show lc-cluster group-membership",
    "show switches",
    "show version",
    "show clock",
    "show log security",
    "show log user",
    "show log network",
    "show log system",
)
#: Refused anywhere in an ``aos8_show_command`` command, even under an allowed prefix: text, then whole words.
_DENIED_TEXT = ("running-config", "startup-config", "tech-support", "crypto", "mgmt-user")
_DENIED_WORDS = frozenset({"configuration", "password", "secret", "key", "passphrase", "psk", "certificate"})
_COMMAND_CHARS = re.compile(r"^[A-Za-z0-9 _.:/@,-]+$")
_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
_USERNAME = re.compile(r"^[A-Za-z0-9_.@\\-]{1,64}$")
_CONFIG_PATH = re.compile(r"^/(md|mm)(/[A-Za-z0-9_.:-]{1,64}){0,10}$")
_SECRET_TEXT = re.compile(r"(?i)\b(key|secret|passphrase|password|psk|community)(\s*[:=]\s*)\S+")
_SECRET_PARAMETER = re.compile(r"(?i)key|secret|passphrase|password|psk|community")

#: Config objects ``aos8_get_config_object`` reads (``GET /v1/configuration/object/<name>``): roles and auth.
CONFIG_OBJECTS = (
    "role",
    "acl_sess",
    "aaa_prof",
    "server_group_prof",
    "rad_server",
    "dot1x_auth_profile",
    "mac_auth_profile",
    "cp_auth_profile",
    "virtual_ap",
    "ap_group",
    "vlan_id",
)
ConfigObject = Literal[
    "role",
    "acl_sess",
    "aaa_prof",
    "server_group_prof",
    "rad_server",
    "dot1x_auth_profile",
    "mac_auth_profile",
    "cp_auth_profile",
    "virtual_ap",
    "ap_group",
    "vlan_id",
]

Mac = Annotated[str, Field(description="The client's MAC address, any format (00:00:5e:00:53:01, 00005E005301).")]
ConfigPath = Annotated[
    str, Field(description="The AOS 8 configuration node: /md (all managed devices, the default), /md/<group>, /mm.")
]
Limit = Annotated[int, Field(description="Items to return, 1-200.")]
Offset = Annotated[int, Field(description="Items to skip, for the next page.")]

#: Column names AOS 8 uses (folded: lower case, letters and digits only) -> the name a reply uses.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "mac": ("mac", "macaddress", "macaddr", "clientmac", "stamac"),
    "ip": ("ip", "ipaddress", "ipaddr", "clientip"),
    "user": ("name", "username", "user"),
    "role": ("role", "userrole"),
    "vlan": ("vlan", "vlanid", "currentvlan"),
    "ap": ("apname", "ap"),
    "essid": ("essid", "ssid"),
    "bssid": ("bssid",),
    "phy": ("phy", "phytype"),
    "auth": ("auth", "authentication", "authtype", "authmethod", "method"),
    "auth_server": ("authserver", "server", "servergroup", "authservername", "radiusserver"),
    "aaa_profile": ("profile", "aaaprofile"),
    "age": ("agedhm", "agedhms", "age", "uptime"),
    "forward_mode": ("forwardmode",),
    "device_type": ("type", "devicetype"),
    "host_name": ("hostname",),
    "user_type": ("usertype",),
    "roaming": ("roaming",),
    "controller": ("currentswitch", "switchip", "switch", "controller"),
    "rssi": ("rssi", "signal", "snr"),
    "health": ("health", "clienthealth"),
}
_FOLDED_TO_FIELD = {alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases}
_FIND_FIELDS = ("mac", "ip", "user", "role", "vlan", "ap", "essid", "bssid", "phy", "auth", "auth_server",
                "aaa_profile", "age")  # fmt: skip
_HISTORY_ALIASES = {
    "time": ("time", "timestamp", "date", "datetime"),
    "event": ("event", "type", "action", "status"),
    "reason": ("reason", "reasoncode", "deauthreason", "description"),
    "ap": ("apname", "ap"),
    "bssid": ("bssid",),
    "essid": ("essid", "ssid"),
    "phy": ("phy", "radio", "band"),
}

#: Auth trace steps that tell the story, by what they mean.
_TRACE_EVENTS = {
    "radius_accept": ("rad-accept",),
    "radius_reject": ("rad-reject",),
    "radius_timeout": ("rad-timeout", "rad-resp-timeout", "server-timeout"),
    "eap_success": ("eap-success",),
    "eap_failure": ("eap-failure",),
    "station_up": ("station-up",),
    "station_down": ("station-down", "station-del"),
    "deauth": ("deauth",),
}
_OUTCOMES = ("radius_accept", "radius_reject", "radius_timeout", "eap_success", "eap_failure")


class Aos8CommandError(ToolError):
    """The controller refused a show command (``% Invalid input``) or answered an error status."""

    def __init__(self, command: str, message: str) -> None:
        self.command = command
        self.message = message
        super().__init__(f"AOS 8 refused {command!r}: {message}")

    def as_error(self) -> dict[str, Any]:
        return {"error": str(self), "product": "aos8", "command": self.command, "code": "command_refused"}


# ── checks ──────────────────────────────────────────────────────────────────


def _mac(value: str) -> str:
    """``00:00:5e:00:53:01`` from any common spelling, or ``ValueError``."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", value or "").lower()
    if len(digits) != 12 or re.fullmatch(r"[0-9A-Fa-f:.\- ]+", value or "") is None:
        raise ValueError("mac must be a MAC address with exactly 12 hex digits")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        raise ValueError("ip must be an IPv4 or IPv6 address") from None


def _username(value: str) -> str:
    value = (value or "").strip()
    if not _USERNAME.match(value):
        raise ValueError("username may hold letters, digits and _ . @ \\ - only (at most 64)")
    return value


def _name(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _NAME.match(value):
        raise ValueError(f"{what} may hold letters, digits and _ . : @ - only (at most 64)")
    return value


def _config_path(value: str) -> str:
    value = (value or "/md").strip().rstrip("/") or "/md"
    if not _CONFIG_PATH.match(value) or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("config_path must be /md, /mm or a node under them, such as /md/Branch")
    return value


def check_show_command(command: str) -> str:
    """``command`` as one clean ``show`` line on the allow-list, or ``ValueError`` saying why not."""
    if not isinstance(command, str):
        raise ValueError("command must be text")  # noqa: TRY004 - one error type for the tool
    if any(c in command for c in ";|\n\r`$<>&"):
        raise ValueError("command must be one show command: no ';', '|', redirects or new lines")
    line = " ".join(command.split())
    if len(line) > 200 or not _COMMAND_CHARS.match(line or "?"):
        raise ValueError("command may hold letters, digits, spaces and _ . : / @ , - only (at most 200)")
    low = line.lower()
    if not low.startswith("show "):
        raise ValueError("only show commands are run")
    words = set(re.split(r"[ ,]+", low))
    denied = [w for w in _DENIED_TEXT if w in low] + sorted(words & _DENIED_WORDS)
    if denied:
        raise ValueError(f"not run: {denied[0]!r} can show the running config or a secret")
    if not any(low == p or low.startswith(p + " ") for p in ALLOWED_SHOW_PREFIXES):
        raise ValueError("not an allowed show command; allowed: " + ", ".join(ALLOWED_SHOW_PREFIXES))
    return line


# ── reading a showcommand reply ─────────────────────────────────────────────


def _ao() -> Any:
    return client("aos8")


def _fold(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def _text_lines(data: Any) -> list[str]:
    """The free-text lines of a reply (``_data``), split on new lines, blank ones dropped."""
    raw = data.get("_data") if isinstance(data, dict) else data if isinstance(data, (list, str)) else None
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.extend(line.rstrip() for line in item.splitlines() if line.strip())
    return out


def _tables(data: Any) -> dict[str, list[dict[str, Any]]]:
    """Every table in a reply: ``{title: rows}`` for each top-level list of objects."""
    if not isinstance(data, dict):
        return {}
    return {
        str(key): [row for row in value if isinstance(row, dict)]
        for key, value in data.items()
        if not str(key).startswith("_") and isinstance(value, list) and any(isinstance(r, dict) for r in value)
    }


def _main_rows(data: Any) -> list[dict[str, Any]]:
    tables = _tables(data)
    return max(tables.values(), key=len) if tables else []


def _scrub_line(line: str) -> str:
    return _SECRET_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{HIDDEN}", line)


def _short(value: Any) -> Any:
    if isinstance(value, str):
        value = _scrub_line(value.strip())
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "..."
    return value


def _scrub_row(row: dict[str, Any]) -> dict[str, Any]:
    """A row with a ``Parameter``/``Value`` secret hidden (profile details are shown that way)."""
    param = next((v for k, v in row.items() if _fold(k) == "parameter"), None)
    out = {k: _short(v) for k, v in row.items()}
    if isinstance(param, str) and _SECRET_PARAMETER.search(param):
        for key in out:
            if _fold(key) == "value":
                out[key] = HIDDEN
    return out


def _refusal(lines: list[str]) -> str | None:
    for line in lines[:5]:
        if line.lstrip().startswith("%"):
            return line.strip().lstrip("% ").strip() or "invalid command"
    return None


def _check_result(command: str, data: Any) -> None:
    if not isinstance(data, dict):
        return
    result = data.get("_global_result")
    if isinstance(result, dict) and str(result.get("status", "0")).strip().lower() not in ("0", "", "success", "ok"):
        raise Aos8CommandError(command, str(result.get("status_str") or result.get("status") or "error"))
    refused = _refusal(_text_lines(data))
    if refused is not None:
        raise Aos8CommandError(command, refused)


async def _show(command: str) -> Any:
    """Run one ``show`` command built here (its pieces already checked) and return the reply."""
    data = await _ao().request("GET", SHOW_PATH, params={"command": command}, kind="read")
    _check_result(command, data)
    return data if data is not None else {}


async def _try_show(command: str) -> Any | None:
    """Like :func:`_show`, but a command this controller does not know answers ``None``."""
    try:
        return await _show(command)
    except Aos8CommandError:
        return None


def _bounded_lines(lines: list[str], limit: int, *, newest_last: bool = False) -> dict[str, Any]:
    """Text lines, at most ``limit``; with ``newest_last`` the last ones are kept (a trace buffer)."""
    lim = clamp_limit(limit)
    kept = lines[-lim:] if newest_last else lines[:lim]
    return {"lines": [_scrub_line(line) for line in kept], "total_lines": len(lines), "truncated": len(lines) > lim}


def _bounded_rows(rows: list[dict[str, Any]], limit: int, offset: int, key: str) -> dict[str, Any]:
    out = bound_collection_response({key: rows}, limit=limit, offset=offset, list_key=key)
    return out if isinstance(out, dict) else {key: rows}


# ── client rows ─────────────────────────────────────────────────────────────


def _split_essid_bssid_phy(value: Any) -> dict[str, str]:
    """``Corp/00:00:5e:00:53:f0/5GHz-HE`` -> essid, bssid, phy (an ESSID may itself hold ``/``)."""
    if not isinstance(value, str) or value.count("/") < 2:
        return {}
    *essid, bssid, phy = value.split("/")
    return {k: v for k, v in (("essid", "/".join(essid)), ("bssid", bssid), ("phy", phy)) if v}


def _client_row(row: dict[str, Any], *, keep_other: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    other: dict[str, Any] = {}
    for key, value in row.items():
        if value in (None, "", [], {}):
            continue
        folded = _fold(key)
        if folded == "essidbssidphy":
            for name, part in _split_essid_bssid_phy(value).items():
                out.setdefault(name, part)
            continue
        field = _FOLDED_TO_FIELD.get(folded)
        if field is not None and field not in out:
            out[field] = _short(value)
        elif keep_other and len(other) < MAX_OTHER_FIELDS:
            other[str(key)] = _short(value)
    if isinstance(out.get("mac"), str):
        try:
            out["mac"] = _mac(out["mac"])
        except ValueError:
            pass
    if other:
        out["other"] = other
    return out


def _find_row(row: dict[str, Any]) -> dict[str, Any]:
    full = _client_row(row)
    return {k: full[k] for k in _FIND_FIELDS if k in full}


def _matches(row: dict[str, Any], selector: str, value: str) -> bool:
    got = row.get({"mac": "mac", "ip": "ip", "name": "user"}[selector])
    if not isinstance(got, str):
        return False
    return got.lower() == value.lower()


#: ``show user mac <mac>`` text: what each line says about how the client got its role and VLAN.
_DETAIL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role", re.compile(r"(?i)\bRole:\s*([^\s,(]+)")),
    ("role_derivation", re.compile(r"(?i)\bRole:\s*[^\s,(]+\s*\(how:\s*([^),]+)")),
    ("auth_status", re.compile(r"(?i)\bAuthentication:\s*[^,]*,\s*status:\s*([^,]+)")),
    ("auth_method", re.compile(r"(?i)\bmethod:\s*([^,]+)")),
    ("auth_protocol", re.compile(r"(?i)\bprotocol:\s*([^,]+)")),
    ("auth_server", re.compile(r"(?i)\bserver:\s*([^,\s]+)")),
    ("vlan", re.compile(r"(?i)\bVlan\b.*?\bCurrent:\s*(\d+)")),
    ("vlan_derivation", re.compile(r"(?i)\bvlan-how:\s*(\S+)")),
    ("essid", re.compile(r"(?i)\bEssid:\s*([^,]+)")),
    ("bssid", re.compile(r"(?i)\bBssid:\s*([0-9a-f:]{17})")),
    ("ap", re.compile(r"(?i)\bAP name/group:\s*([^/\s]+)")),
    ("ap_group", re.compile(r"(?i)\bAP name/group:\s*[^/\s]+/(\S+)")),
    ("phy", re.compile(r"(?i)\bPhy:\s*([^,\s]+)")),
    ("rssi", re.compile(r"(?i)\bRSSI:\s*(-?\d+)")),
    ("aaa_profile", re.compile(r"(?i)\bAAA prof:\s*([^,\s]+)")),
    ("auth_profile", re.compile(r"(?i)\b(?:dot1x|802\.1x|mac auth) prof(?:ile)?:\s*([^,\s]+)")),
)


def _detail_from_text(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        for field, pattern in _DETAIL_PATTERNS:
            if field in out:
                continue
            match = pattern.search(line)
            if match:
                out[field] = match.group(1).strip()
    return out


# ── reads ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def aos8_find_client(
    mac: str | None = None, ip: str | None = None, username: str | None = None, limit: Limit = 20
) -> dict[str, Any]:
    """Find a wireless or wired client on an AOS 8 controller by MAC, IP or username (give exactly one).

    Runs ``show user-table mac|ip|name <value>``; when the controller has no such client
    (a Mobility Conductor holds none itself) it searches ``show global-user-table list``.
    Each match: mac, ip, user, role, vlan, ap, essid, bssid, phy, auth, auth_server,
    aaa_profile and age. Next: aos8_get_client_detail for how the role was derived.
    """
    given = [(k, v) for k, v in (("mac", mac), ("ip", ip), ("name", username)) if v not in (None, "")]
    if len(given) != 1:
        raise ValueError("give exactly one of mac, ip or username")
    selector, raw = given[0]
    value = _mac(raw or "") if selector == "mac" else _ip(raw or "") if selector == "ip" else _username(raw or "")
    lim = clamp_limit(limit, default=20)
    command = f"show user-table {selector} {value}"
    rows = [_find_row(r) for r in _main_rows(await _show(command))]
    rows = [r for r in rows if _matches(r, selector, value)] or rows
    source = "user-table"
    complete = True
    if not rows:
        command = "show global-user-table list"
        data = await _try_show(command)
        if data is not None:
            source = "global-user-table"
            all_rows = [_find_row(r) for r in _main_rows(data)]
            rows = [r for r in all_rows if _matches(r, selector, value)]
            complete = not (isinstance(data, dict) and "_response_bounds" in data)
    out: dict[str, Any] = {"query": {selector if selector != "name" else "username": value}, "source": source,
                           "found": len(rows), "clients": rows[:lim]}  # fmt: skip
    if not complete:
        out["complete"] = False
        out["note"] = "the global user table was too large to read whole; search on the managed controller"
    if not rows:
        out["note"] = out.get("note") or "no such client on this controller now (it may have left, or be on another)"
    return out


@mcp.tool()
async def aos8_list_clients(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    """Clients on an AOS 8 controller now (``show user-table``): mac, ip, user, role, ap, essid, auth, age."""
    rows = [_find_row(r) for r in _main_rows(await _show("show user-table"))]
    return _bounded_rows(rows, limit, offset, "clients")


@mcp.tool()
async def aos8_get_client_detail(mac: Mac) -> dict[str, Any]:
    """One AOS 8 client in full: role and how it was derived, VLAN, auth method and server, ESSID, AP, RSSI.

    Reads ``show user-table verbose mac <mac>`` and ``show user mac <mac>``. ``role_derivation``
    says how the role was set (for example ROLE_DERIVATION_DOT1X: from the RADIUS server;
    a default role means the server sent none), ``auth_server`` which RADIUS server answered.
    """
    value = _mac(mac)
    command = f"show user-table verbose mac {value}"
    data = await _try_show(command)
    if data is None:
        command = f"show user-table mac {value}"
        data = await _show(command)
    rows = [_client_row(r, keep_other=True) for r in _main_rows(data)]
    rows = [r for r in rows if r.get("mac") in (None, value)] or rows
    detail: dict[str, Any] = dict(rows[0]) if rows else {}
    user = await _try_show(f"show user mac {value}")
    lines = _text_lines(user) if user is not None else []
    if user is not None and not lines:
        lines = [f"{k}: {v}" for r in _main_rows(user) for k, v in r.items() if isinstance(v, (str, int))]
    for field, found in _detail_from_text(lines).items():
        if field not in detail or field in ("role_derivation", "vlan_derivation", "auth_server"):
            detail[field] = _short(found)
    out: dict[str, Any] = {"mac": value, "found": bool(rows or lines), "client": detail}
    if lines:
        out["user_detail"] = _bounded_lines(lines, 60)
    if not out["found"]:
        out["note"] = "no such client on this controller now; try aos8_find_client or aos8_get_client_history"
    return out


@mcp.tool()
async def aos8_get_client_history(mac: Mac, limit: Limit = 50) -> dict[str, Any]:
    """A client's AP association history on AOS 8: when, which AP and BSSID, associate/disassociate, reason.

    Reads ``show ap client trail-info <mac>``; on a controller without it,
    ``show ap association client-mac <mac>`` (the current association only).
    """
    value = _mac(mac)
    command = f"show ap client trail-info {value}"
    data = await _try_show(command)
    if data is None:
        command = f"show ap association client-mac {value}"
        data = await _show(command)
    tables = _tables(data)
    events: list[dict[str, Any]] = []
    for rows in tables.values():
        for row in rows:
            entry: dict[str, Any] = {}
            for key, val in row.items():
                folded = _fold(key)
                field = next((f for f, aliases in _HISTORY_ALIASES.items() if folded in aliases), None)
                if field and field not in entry and val not in (None, ""):
                    entry[field] = _short(val)
            events.append(entry or {k: _short(v) for k, v in row.items()})
    out: dict[str, Any] = {"mac": value, "command": command}
    if events:
        out.update(_bounded_rows(events, limit, 0, "events"))
    lines = _text_lines(data)
    if lines:
        out["text"] = _bounded_lines(lines, limit)
    if not events and not lines:
        out["events"] = []
    return out


@mcp.tool()
async def aos8_auth_trace(mac: Mac, limit: Limit = 40) -> dict[str, Any]:
    """Why a client failed (or passed) authentication on an AOS 8 controller: its auth trace buffer.

    Reads ``show auth-tracebuf mac <mac>``: the 802.1X / EAP, MAC-auth and RADIUS steps
    (rad-req, rad-accept, rad-reject, timeouts, eap-failure, station up and down), newest
    last. ``summary`` counts each outcome and names the last one. The controller's
    counterpart of ClearPass Access Tracker: a reject points at the RADIUS server's policy,
    a timeout at reachability or the shared secret.
    """
    value = _mac(mac)
    command = f"show auth-tracebuf mac {value}"
    data = await _show(command)
    lines = _text_lines(data)
    rows = _main_rows(data)
    entries = lines or [" ".join(str(v) for v in r.values() if v not in (None, "")) for r in rows]
    counts = {name: 0 for name in _TRACE_EVENTS}
    last: str | None = None
    for entry in entries:
        low = entry.lower()
        for name, words in _TRACE_EVENTS.items():
            if any(w in low for w in words):
                counts[name] += 1
                if name in _OUTCOMES:
                    last = name
    summary: dict[str, Any] = {name: n for name, n in counts.items() if n}
    summary["last_outcome"] = last
    out: dict[str, Any] = {"mac": value, "summary": summary}
    if lines or not rows:
        out["trace"] = _bounded_lines(lines, limit, newest_last=True)
    else:
        lim = clamp_limit(limit)
        out["trace"] = {"rows": [_scrub_row(r) for r in rows[-lim:]], "total": len(rows), "truncated": len(rows) > lim}
    if not entries:
        out["note"] = "the trace buffer has nothing for this MAC (it holds recent attempts only)"
    return out


def _role(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    name = item.get("rname") or item.get("name")
    rest = {k: v for k, v in item.items() if k not in ("rname", "name")}
    return {"name": name, **rest} if name else rest


async def _object(name: str, config_path: str) -> list[Any]:
    path = _config_path(config_path)
    data = await _ao().request("GET", f"{OBJECT_PATH}/{name}", params={"config_path": path}, kind="read")
    _check_result(f"object {name}", data)
    body = data.get("_data", data) if isinstance(data, dict) else data
    found = body.get(name) if isinstance(body, dict) else body
    if isinstance(found, dict):
        return [found]
    return list(found) if isinstance(found, list) else []


@mcp.tool()
async def aos8_list_user_roles(
    config_path: ConfigPath = "/md", limit: Limit = 50, offset: Offset = 0
) -> dict[str, Any]:
    """AOS 8 user roles (``GET /v1/configuration/object/role``): name, ACLs, VLAN, captive portal."""
    roles = [_role(r) for r in await _object("role", config_path)]
    return {"config_path": _config_path(config_path), **_bounded_rows(roles, limit, offset, "roles")}


@mcp.tool()
async def aos8_get_config_object(
    object_name: ConfigObject, config_path: ConfigPath = "/md", limit: Limit = 50, offset: Offset = 0
) -> dict[str, Any]:
    """One kind of AOS 8 role or auth configuration (``GET /v1/configuration/object/<name>``), secrets hidden.

    object_name: role, acl_sess (session ACLs / policies), aaa_prof (AAA profiles),
    server_group_prof, rad_server (RADIUS servers), dot1x_auth_profile, mac_auth_profile,
    cp_auth_profile (captive portal), virtual_ap, ap_group or vlan_id.
    """
    if object_name not in CONFIG_OBJECTS:
        raise ValueError("object_name must be one of: " + ", ".join(CONFIG_OBJECTS))
    items = await _object(object_name, config_path)
    return {"object": object_name, "config_path": _config_path(config_path),
            **_bounded_rows(items, limit, offset, "items")}  # fmt: skip


def _params_or_rows(data: Any, limit: int) -> dict[str, Any]:
    """A profile's detail: its ``Parameter``/``Value`` rows as one object, other tables as rows."""
    out: dict[str, Any] = {}
    for title, rows in _tables(data).items():
        scrubbed = [_scrub_row(r) for r in rows]
        keys = {_fold(k) for r in rows for k in r}
        if {"parameter", "value"} <= keys:
            params: dict[str, Any] = {}
            for row in scrubbed:
                param = next((v for k, v in row.items() if _fold(k) == "parameter"), None)
                val = next((v for k, v in row.items() if _fold(k) == "value"), None)
                if isinstance(param, str) and param:
                    params[param] = val
            out[title] = params
        else:
            out[title] = scrubbed[: clamp_limit(limit)]
            if len(scrubbed) > clamp_limit(limit):
                out[f"{title}_truncated"] = True
    lines = _text_lines(data)
    if lines:
        out["text"] = _bounded_lines(lines, limit)
    return out


@mcp.tool()
async def aos8_list_aaa_profiles(name: str | None = None, limit: Limit = 50) -> dict[str, Any]:
    """AAA profiles on AOS 8 (``show aaa profile``), or one profile's settings with ``name``.

    A profile ties an SSID to its 802.1X and MAC-auth profiles, the RADIUS server group for
    each, and the initial and default roles. A client's ``aaa_profile`` (from
    aos8_find_client) is the name to pass.
    """
    command = "show aaa profile" if name in (None, "") else f"show aaa profile {_name(name or '', 'name')}"
    return {"command": command, **_params_or_rows(await _show(command), limit)}


@mcp.tool()
async def aos8_list_aaa_servers(server_group: str | None = None, limit: Limit = 50) -> dict[str, Any]:
    """Which RADIUS servers (for example ClearPass) an AOS 8 controller authenticates against.

    ``show aaa server-group`` (or one group with ``server_group``: its servers in order,
    fail-through, load balancing) and ``show aaa authentication-server radius`` (each
    server's host, ports and in-service state). Shared secrets are never shown.
    """
    group = None if server_group in (None, "") else _name(server_group or "", "server_group")
    groups_command = "show aaa server-group" if group is None else f"show aaa server-group {group}"
    out: dict[str, Any] = {"server_groups": _params_or_rows(await _show(groups_command), limit)}
    radius = await _try_show("show aaa authentication-server radius")
    if radius is not None:
        out["radius_servers"] = _params_or_rows(radius, limit)
    out["commands"] = [groups_command, "show aaa authentication-server radius"]
    return out


@mcp.tool()
async def aos8_show_command(command: str, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    """Run one read-only AOS 8 ``show`` command from an allow-list and return its tables or text.

    Allowed: user-table, user, global-user-table, station-table, auth-tracebuf, ap database /
    active / details / association / client trail-info / bss-table / radio-summary / arm history,
    ap-group, wlan virtual-ap, aaa profile / server-group / authentication-server / authentication
    dot1x|mac|captive-portal / state / derivation-rules, rights, netdestination, ip access-list,
    vlan, ip interface brief, datapath user, lc-cluster group-membership, switches, version, clock,
    log security|user|network|system. One line; no ``;`` or ``|``; nothing that shows the running
    config or a secret.
    """
    line = check_show_command(command)
    data = await _show(line)
    out: dict[str, Any] = {"command": line}
    tables = _tables(data)
    for title, rows in tables.items():
        scrubbed = [_scrub_row(r) for r in rows]
        bounded = bound_collection_response({title: scrubbed}, limit=limit, offset=offset, list_key=title)
        out[title] = bounded[title] if isinstance(bounded, dict) else scrubbed
        if isinstance(bounded, dict) and "_pagination" in bounded:
            out.setdefault("pages", {})[title] = bounded["_pagination"]
    lines = _text_lines(data)
    if lines or not tables:
        out["text"] = _bounded_lines(
            lines, limit, newest_last=line.lower().startswith(("show auth-tracebuf", "show log"))
        )
    return out


# ── the backend ─────────────────────────────────────────────────────────────


def backend(client: ClientGetter | None = None) -> MCPServer:
    """The ``aos8`` backend: every hand-written AOS 8 tool, labelled from ``labels.yaml``."""
    if client is not None:
        use_client("aos8", client)
    server = MCPServer("aos8")
    mcp.register(server)
    return server


def backend_tools() -> list[Any]:
    """The registered AOS 8 tools (``.name``, ``.fn``, ``.parameters``)."""
    return list(sdk_compat.tool_registry(backend()).values())
