"""AOS 8 tools against a fake Mobility Conductor: session reuse, re-login, logout, reply shaping, refusals.

The replies follow the AOS 8 showcommand shapes: a table as ``{"<Title>": [rows], "_meta": [columns]}``
and free text as ``{"_data": [lines]}``. Sample data with documentation addresses, not from a real network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import AOS8_BASE, Backend

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.http import aclose_pooled_clients
from casper_network_mcp.core.redact import redact_tool_error_text
from casper_network_mcp.products.aos8 import tools as aos8
from casper_network_mcp.products.aos8.client import Aos8Client

PASSWORD = "test-secret-do-not-log"
MAC = "00:00:5e:00:53:01"

USER_TABLE_COLUMNS = ["IP", "MAC", "Name", "Role", "Age(d:h:m)", "Auth", "VPN link", "AP name", "Roaming",
                      "Essid/Bssid/Phy", "Profile", "Forward mode", "Type", "Host Name", "User Type"]  # fmt: skip
USER_ROW = {
    "IP": "192.0.2.10",
    "MAC": MAC,
    "Name": "user1",
    "Role": "employee",
    "Age(d:h:m)": "00:01:12",
    "Auth": "802.1x",
    "VPN link": None,
    "AP name": "AP-Lobby",
    "Roaming": "Wireless",
    "Essid/Bssid/Phy": "Corp/00:00:5e:00:53:f0/5GHz-HE",
    "Profile": "Corp-aaa",
    "Forward mode": "tunnel",
    "Type": "Win 10",
    "Host Name": "laptop-1",
    "User Type": "WIRELESS",
}
SHOW_USER_LINES = [
    "This operation can take a while depending on number of users. Please be patient ....",
    "Name: user1, IP: 192.0.2.10, MAC: 00:00:5e:00:53:01, Age: 00:01:12",
    "Role: employee (how: ROLE_DERIVATION_DOT1X), ACL: 81/0",
    "Authentication: Yes, status: successful, method: 802.1x, protocol: EAP-PEAP, server: cppm-01",
    "Vlan default: 10, Assigned: 20, Current: 20 vlan-how: VLAN_DERIVATION_DOT1X",
    "Essid: Corp, Bssid: 00:00:5e:00:53:f0 AP name/group: AP-Lobby/Campus Phy: a-HE-80, RSSI: 41",
]
AUTH_TRACE_LINES = [
    "Auth Trace Buffer",
    "-----------------",
    "Nov  9 10:01:02  station-up      *  00:00:5e:00:53:01  00:00:5e:00:53:f0  -  -  wpa3 aes",
    "Nov  9 10:01:02  eap-id-req     <-  00:00:5e:00:53:01  00:00:5e:00:53:f0  1  5",
    "Nov  9 10:01:03  rad-req        ->  00:00:5e:00:53:01  00:00:5e:00:53:f0  12  276  cppm-01",
    "Nov  9 10:01:05  rad-reject     <-  00:00:5e:00:53:01  00:00:5e:00:53:f0  12  44  cppm-01",
    "Nov  9 10:01:05  eap-failure    <-  00:00:5e:00:53:01  00:00:5e:00:53:f0  2  4",
]


class FakeController(httpx.MockTransport):
    """A fake Mobility Conductor: login, logout, showcommand by command, objects by name."""

    def __init__(self) -> None:
        super().__init__(self._handle)
        self.shows: dict[str, Any] = {}
        self.objects: dict[str, Any] = {}
        self.logins: list[httpx.Request] = []
        self.logouts: list[httpx.Request] = []
        self.calls: list[httpx.Request] = []
        self.login_reply: Any = None
        self.expire_next = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/api/login":
            self.logins.append(request)
            if self.login_reply is not None:
                return self.login_reply
            uid = f"uid-{len(self.logins)}"
            body = {"_global_result": {"status": "0", "status_str": "You've logged in successfully.", "UIDARUBA": uid}}
            return httpx.Response(200, json=body, headers={"Set-Cookie": f"SESSION=cookie-{uid}; Path=/; Secure"})
        if path == "/v1/api/logout":
            self.logouts.append(request)
            return httpx.Response(200, json={"_global_result": {"status": "0", "status_str": "logged out"}})
        self.calls.append(request)
        if self.expire_next:
            self.expire_next -= 1
            return httpx.Response(401, json={"_global_result": {"status": "1", "status_str": "Unauthorized"}})
        if path == "/v1/configuration/showcommand":
            command = request.url.params.get("command", "")
            reply = self.shows.get(command)
            if reply is None:
                reply = {"_data": ["% Invalid input detected at '^' marker."]}
            return httpx.Response(200, json=reply)
        if path.startswith("/v1/configuration/object/"):
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"_data": {name: self.objects.get(name, [])}})
        return httpx.Response(404, json={"detail": "no such path"})

    def commands(self) -> list[str]:
        return [r.url.params.get("command", "") for r in self.calls if r.url.path.endswith("showcommand")]


@pytest.fixture
def controller() -> FakeController:
    return FakeController()


def _client(controller: FakeController, *, read_only: bool = False, **kw: Any) -> Aos8Client:
    args: dict[str, Any] = {"base_url": AOS8_BASE, "username": "admin", "password": PASSWORD, **kw}
    return Aos8Client(gate=Gate(read_only), transport=controller, **args)


@pytest.fixture
def make_backend(controller):
    def make(**kw: Any) -> Backend:
        client = _client(controller, **kw)
        return Backend(aos8.backend(client=lambda: client))

    return make


def _no_secrets(out: Any) -> None:
    text = json.dumps(out, default=str)
    assert PASSWORD not in text
    assert "uid-" not in text and "cookie-uid" not in text


# ── the session ─────────────────────────────────────────────────────────────


async def test_logs_in_once_and_reuses_the_session(controller, make_backend):
    controller.shows["show user-table"] = {"Users": [USER_ROW], "_meta": USER_TABLE_COLUMNS}
    backend = make_backend()
    await backend.call("aos8_list_clients", {})
    out = await backend.call("aos8_list_clients", {})
    assert len(controller.logins) == 1
    login = controller.logins[0]
    assert login.method == "POST" and login.url.port == 4343
    assert dict(httpx.QueryParams(login.content.decode())) == {"username": "admin", "password": PASSWORD}
    for call in controller.calls:
        assert call.method == "GET"
        assert call.url.params["UIDARUBA"] == "uid-1"
        assert call.headers["Cookie"] == "SESSION=cookie-uid-1"
    _no_secrets(out)


async def test_an_expired_session_logs_in_again_once(controller, make_backend):
    controller.shows["show user-table"] = {"Users": [USER_ROW]}
    backend = make_backend()
    await backend.call("aos8_list_clients", {})
    controller.expire_next = 1
    out = await backend.call("aos8_list_clients", {})
    assert out["clients"][0]["mac"] == MAC
    assert len(controller.logins) == 2
    assert controller.calls[-1].url.params["UIDARUBA"] == "uid-2"


async def test_a_refused_login_is_login_expired_and_hides_the_password(controller, make_backend):
    controller.login_reply = httpx.Response(401, json={"_global_result": {"status": "1", "status_str": "Unauthorized"}})
    out = await make_backend().call("aos8_list_clients", {})
    assert out == {"error": "login_expired", "product": "aos8"}
    assert controller.calls == []


async def test_a_login_reply_without_a_session_id_is_refused(controller, make_backend):
    controller.login_reply = httpx.Response(200, json={"_global_result": {"status": "1", "status_str": "Login failed"}})
    out = await make_backend().call("aos8_list_clients", {})
    assert out == {"error": "login_expired", "product": "aos8"}


async def test_logout_ends_the_session(controller):
    controller.shows["show user-table"] = {"Users": []}
    client = _client(controller)
    assert await client.logout() is False  # nothing to end yet
    await Backend(aos8.backend(client=lambda: client)).call("aos8_list_clients", {})
    assert client.has_session
    assert await client.logout() is True
    [logout] = controller.logouts
    assert logout.method == "GET" and logout.url.params["UIDARUBA"] == "uid-1"
    assert not client.has_session


async def test_the_server_logs_out_when_it_stops(controller):
    controller.shows["show user-table"] = {"Users": []}
    client = _client(controller)
    await Backend(aos8.backend(client=lambda: client)).call("aos8_list_clients", {})
    await aclose_pooled_clients()
    assert len(controller.logouts) == 1 and not client.has_session


async def test_no_login_is_login_missing(controller):
    client = Aos8Client(gate=Gate(False), base_url=AOS8_BASE, username=None, password=None, transport=controller)
    out = await Backend(aos8.backend(client=lambda: client)).call("aos8_find_client", {"mac": MAC})
    assert out == {"error": "login_missing", "product": "aos8"}
    assert controller.logins == [] and controller.calls == []


async def test_reads_run_with_the_read_only_pin(controller, make_backend):
    controller.shows["show user-table"] = {"Users": [USER_ROW]}
    out = await make_backend(read_only=True).call("aos8_list_clients", {})
    assert out["clients"][0]["user"] == "user1"


def test_session_values_are_hidden_in_error_text():
    text = redact_tool_error_text(
        f"GET {AOS8_BASE}/v1/configuration/showcommand?UIDARUBA=abc123&x=1 Cookie: SESSION=s3cr3t;"
    )
    assert "abc123" not in text and "s3cr3t" not in text
    assert "UIDARUBA=[hidden]" in text and "SESSION=[hidden]" in text


# ── clients ─────────────────────────────────────────────────────────────────


async def test_find_client_by_mac_is_compact(controller, make_backend):
    controller.shows[f"show user-table mac {MAC}"] = {"Users": [USER_ROW], "_meta": USER_TABLE_COLUMNS}
    out = await make_backend().call("aos8_find_client", {"mac": "00005E005301"})
    assert controller.commands() == [f"show user-table mac {MAC}"]
    assert out["source"] == "user-table" and out["found"] == 1
    assert out["clients"] == [
        {"mac": MAC, "ip": "192.0.2.10", "user": "user1", "role": "employee", "ap": "AP-Lobby", "essid": "Corp",
         "bssid": "00:00:5e:00:53:f0", "phy": "5GHz-HE", "auth": "802.1x", "aaa_profile": "Corp-aaa",
         "age": "00:01:12"}
    ]  # fmt: skip


async def test_find_client_by_ip_and_username(controller, make_backend):
    controller.shows["show user-table ip 192.0.2.10"] = {"Users": [USER_ROW]}
    controller.shows["show user-table name user1"] = {"Users": [USER_ROW]}
    backend = make_backend()
    assert (await backend.call("aos8_find_client", {"ip": "192.0.2.10"}))["clients"][0]["mac"] == MAC
    assert (await backend.call("aos8_find_client", {"username": "user1"}))["query"] == {"username": "user1"}


async def test_find_client_falls_back_to_the_global_user_table(controller, make_backend):
    controller.shows[f"show user-table mac {MAC}"] = {"Users": [], "_meta": USER_TABLE_COLUMNS}
    other = {**USER_ROW, "MAC": "00:00:5e:00:53:02", "IP": "192.0.2.11"}
    controller.shows["show global-user-table list"] = {
        "Global Users": [{**USER_ROW, "Current switch": "198.51.100.41"}, other]
    }
    out = await make_backend().call("aos8_find_client", {"mac": MAC})
    assert out["source"] == "global-user-table" and out["found"] == 1
    assert out["clients"][0]["ip"] == "192.0.2.10"


@pytest.mark.parametrize(
    "args",
    [{}, {"mac": MAC, "ip": "192.0.2.10"}, {"mac": "not-a-mac"}, {"ip": "192.0.2.300"}, {"username": "a b"},
     {"username": "x; reload"}],
)  # fmt: skip
async def test_find_client_checks_its_arguments_before_sending(controller, make_backend, args):
    out = await make_backend().call("aos8_find_client", args)
    assert "error" in out
    assert controller.calls == [] and controller.logins == []


async def test_client_detail_says_how_the_role_was_derived(controller, make_backend):
    verbose = {**USER_ROW, "Vlan": 20, "Server": "cppm-01", "Signal": 41, "Bwm": "default"}
    controller.shows[f"show user-table verbose mac {MAC}"] = {"Users": [verbose]}
    controller.shows[f"show user mac {MAC}"] = {"_data": ["\n".join(SHOW_USER_LINES)]}
    out = await make_backend().call("aos8_get_client_detail", {"mac": MAC})
    client = out["client"]
    assert client["role"] == "employee" and client["role_derivation"] == "ROLE_DERIVATION_DOT1X"
    assert client["vlan"] == 20 and client["vlan_derivation"] == "VLAN_DERIVATION_DOT1X"
    assert client["auth_server"] == "cppm-01" and client["auth_protocol"] == "EAP-PEAP"
    assert client["auth"] == "802.1x" and client["essid"] == "Corp" and client["ap_group"] == "Campus"
    assert client["forward_mode"] == "tunnel" and client["other"] == {"Bwm": "default"}
    assert out["user_detail"]["total_lines"] == len(SHOW_USER_LINES)


async def test_client_detail_without_verbose_uses_the_plain_table(controller, make_backend):
    controller.shows[f"show user-table mac {MAC}"] = {"Users": [USER_ROW]}
    out = await make_backend().call("aos8_get_client_detail", {"mac": MAC})
    assert out["found"] and out["client"]["role"] == "employee"
    assert controller.commands() == [
        f"show user-table verbose mac {MAC}",
        f"show user-table mac {MAC}",
        f"show user mac {MAC}",
    ]


async def test_client_detail_for_a_client_that_left(controller, make_backend):
    controller.shows[f"show user-table verbose mac {MAC}"] = {"Users": []}
    controller.shows[f"show user mac {MAC}"] = {"_data": []}
    out = await make_backend().call("aos8_get_client_detail", {"mac": MAC})
    assert out["found"] is False and "note" in out


async def test_client_history_from_trail_info(controller, make_backend):
    trail = [
        {"Timestamp": "Nov  9 10:00:01", "AP name": "AP-Lobby", "BSSID": "00:00:5e:00:53:f0", "Event": "Association",
         "Reason": "-"},
        {"Timestamp": "Nov  9 10:05:44", "AP name": "AP-Lobby", "BSSID": "00:00:5e:00:53:f0", "Event": "Deauth",
         "Reason": "Ageout"},
    ]  # fmt: skip
    controller.shows[f"show ap client trail-info {MAC}"] = {"Client Trail Info": trail}
    out = await make_backend().call("aos8_get_client_history", {"mac": MAC, "limit": 1})
    assert out["events"] == [{"time": "Nov  9 10:00:01", "ap": "AP-Lobby", "bssid": "00:00:5e:00:53:f0",
                              "event": "Association", "reason": "-"}]  # fmt: skip
    assert out["_pagination"]["total"] == 2 and out["_pagination"]["truncated"] is True


async def test_client_history_falls_back_to_the_current_association(controller, make_backend):
    controller.shows[f"show ap association client-mac {MAC}"] = {
        "Association Table": [{"Name": "AP-Lobby", "bssid": "00:00:5e:00:53:f0", "phy": "a-HE"}]
    }
    out = await make_backend().call("aos8_get_client_history", {"mac": MAC})
    assert out["command"] == f"show ap association client-mac {MAC}"
    assert out["events"][0]["bssid"] == "00:00:5e:00:53:f0"


# ── auth ────────────────────────────────────────────────────────────────────


async def test_auth_trace_summarises_a_radius_reject(controller, make_backend):
    controller.shows[f"show auth-tracebuf mac {MAC}"] = {"_data": AUTH_TRACE_LINES}
    out = await make_backend().call("aos8_auth_trace", {"mac": MAC, "limit": 3})
    assert out["summary"]["radius_reject"] == 1 and out["summary"]["eap_failure"] == 1
    assert out["summary"]["last_outcome"] == "eap_failure"
    assert out["trace"]["lines"] == AUTH_TRACE_LINES[-3:]  # newest last, the oldest dropped
    assert out["trace"]["truncated"] is True


async def test_auth_trace_with_nothing_recorded(controller, make_backend):
    controller.shows[f"show auth-tracebuf mac {MAC}"] = {"_data": []}
    out = await make_backend().call("aos8_auth_trace", {"mac": MAC})
    assert out["summary"] == {"last_outcome": None} and "note" in out


async def test_aaa_servers_name_the_radius_servers_and_hide_keys(controller, make_backend):
    controller.shows["show aaa server-group"] = {
        "Server Groups": [{"Name": "cppm-sg", "References": 2, "Fail-through": "No", "Load-balance": "No"}]
    }
    controller.shows["show aaa server-group cppm-sg"] = {
        "Server Group": [{"Parameter": "Fail Through", "Value": "No"}, {"Parameter": "Shared Key", "Value": "abc123"}],
        "Auth Servers": [{"Pri": 1, "Server": "cppm-01", "Type": "Radius", "Match-Type": None, "Trim-FQDN": "No"}],
    }
    controller.shows["show aaa authentication-server radius"] = {
        "RADIUS Server List": [{"Name": "cppm-01", "FQDN": None, "IP addr": "198.51.100.20", "AuthPort": 1812,
                                "AcctPort": 1813, "Status": "Enabled", "Key": "abc123"}]
    }  # fmt: skip
    backend = make_backend()
    out = await backend.call("aos8_list_aaa_servers", {})
    assert out["server_groups"]["Server Groups"][0]["Name"] == "cppm-sg"
    radius = out["radius_servers"]["RADIUS Server List"][0]
    assert radius["IP addr"] == "198.51.100.20" and radius["Key"] == "[hidden]"
    one = await backend.call("aos8_list_aaa_servers", {"server_group": "cppm-sg"})
    assert one["server_groups"]["Server Group"] == {"Fail Through": "No", "Shared Key": "[hidden]"}
    assert one["server_groups"]["Auth Servers"][0]["Server"] == "cppm-01"
    assert "abc123" not in json.dumps(out) + json.dumps(one)


async def test_aaa_profile_detail(controller, make_backend):
    controller.shows["show aaa profile Corp-aaa"] = {
        'AAA Profile "Corp-aaa"': [
            {"Parameter": "Initial role", "Value": "logon"},
            {"Parameter": "802.1X Authentication Profile", "Value": "Corp-dot1x"},
            {"Parameter": "802.1X Authentication Server Group", "Value": "cppm-sg"},
        ]
    }
    out = await make_backend().call("aos8_list_aaa_profiles", {"name": "Corp-aaa"})
    assert out['AAA Profile "Corp-aaa"']["802.1X Authentication Server Group"] == "cppm-sg"
    bad = await make_backend().call("aos8_list_aaa_profiles", {"name": "Corp aaa; show running-config"})
    assert "error" in bad


async def test_user_roles_from_the_config_object(controller, make_backend):
    controller.objects["role"] = [{"rname": "employee", "role__acl": [{"acl_type": "session", "pname": "allowall"}]},
                                  {"rname": "guest"}]  # fmt: skip
    out = await make_backend().call("aos8_list_user_roles", {"limit": 1})
    assert out["roles"] == [{"name": "employee", "role__acl": [{"acl_type": "session", "pname": "allowall"}]}]
    assert out["_pagination"]["total"] == 2
    [call] = controller.calls
    assert call.url.path == "/v1/configuration/object/role" and call.url.params["config_path"] == "/md"


async def test_config_object_hides_the_radius_key(controller, make_backend):
    controller.objects["rad_server"] = [{"rad_server_name": "cppm-01", "rad_host": {"host": "198.51.100.20"},
                                         "rad_key": {"key": "abc123"}}]  # fmt: skip
    out = await make_backend().call(
        "aos8_get_config_object", {"object_name": "rad_server", "config_path": "/md/Campus"}
    )
    assert out["items"][0]["rad_key"] == "[hidden]"
    assert controller.calls[0].url.params["config_path"] == "/md/Campus"
    bad = await make_backend().call("aos8_get_config_object", {"object_name": "role", "config_path": "/md/../x"})
    assert "error" in bad


async def test_a_controller_error_status_is_reported(controller, make_backend):
    controller.shows["show user-table"] = {"_global_result": {"status": "1", "status_str": "Command not supported"}}
    out = await make_backend().call("aos8_list_clients", {})
    assert out["code"] == "command_refused" and "Command not supported" in out["error"]


# ── the show passthrough ────────────────────────────────────────────────────


async def test_show_runs_an_allowed_command_and_hides_secrets_in_text(controller, make_backend):
    controller.shows["show aaa authentication-server radius cppm-01"] = {
        "_data": ['RADIUS Server "cppm-01"', "Host: 198.51.100.20", "Shared secret: abc123", "Retransmits: 3"]
    }
    out = await make_backend().call(
        "aos8_show_command", {"command": "  show aaa   authentication-server radius cppm-01 "}
    )
    assert out["command"] == "show aaa authentication-server radius cppm-01"
    assert "Shared secret: [hidden]" in out["text"]["lines"] and "Host: 198.51.100.20" in out["text"]["lines"]
    assert "abc123" not in json.dumps(out)


async def test_show_bounds_tables(controller, make_backend):
    rows = [{**USER_ROW, "MAC": f"00:00:5e:00:53:{i:02x}"} for i in range(5)]
    controller.shows["show user-table"] = {"Users": rows, "_meta": USER_TABLE_COLUMNS}
    out = await make_backend().call("aos8_show_command", {"command": "show user-table", "limit": 2, "offset": 1})
    assert [r["MAC"] for r in out["Users"]] == ["00:00:5e:00:53:01", "00:00:5e:00:53:02"]
    assert out["pages"]["Users"]["total"] == 5


@pytest.mark.parametrize(
    "command",
    [
        "show running-config",
        "show configuration committed /md",
        "show user-table; reload",
        "show user-table | include employee",
        "show user-table\nreload",
        "configure terminal",
        "write memory",
        "show tech-support",
        "show crypto isakmp sa",
        "show mgmt-user",
        "show aaa authentication-server radius cppm-01 key",
        "show ip route",
        "show user-table `reload`",
        "",
    ],
)
async def test_show_refuses_anything_else_before_sending(controller, make_backend, command):
    out = await make_backend().call("aos8_show_command", {"command": command})
    assert "error" in out
    assert controller.calls == [] and controller.logins == []


def test_every_allowed_prefix_passes_the_check():
    for prefix in aos8.ALLOWED_SHOW_PREFIXES:
        assert aos8.check_show_command(prefix) == prefix
