"""clearpass_access_tracker: Access Tracker from ClearPass's database, on a fake pg8000 connection.

Synthetic data only (documentation MACs and RFC 5737 addresses); nothing reaches a network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import Backend
from pg8000.exceptions import DatabaseError, InterfaceError

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import labels
from casper_network_mcp.products.clearpass import access_tracker as at
from casper_network_mcp.products.clearpass import tools
from casper_network_mcp.products.clearpass.client import ClearPassClient
from casper_network_mcp.router import find

PASSWORD = "synthetic-db-password-7Q"
MAC = "02:00:00:00:00:01"
NORMALIZED = "020000000001"
NOW = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)


def row(**over):
    base = {
        "session_id": "R00000001-01-00000001",
        "timestamp": datetime(2025, 1, 2, 11, 30, tzinfo=UTC),
        "login_status": "REJECT",
        "source": "RADIUS",
        "service": "Corp 802.1X",
        "user": "test-user",
        "mac": NORMALIZED,
        "nas_name": "nad-1",
        "nas_ip": "192.0.2.10",
        "nas_port": "12",
        "error_code": "206",
        "auth_type": "EAP-PEAP",
        "roles": "[Employee], [Other], [User Authenticated]",
        "enforcement_profiles": "[Deny Access Profile]",
        "monitor_mode": "0",
        "alerts": ["Applied 'Reject' profile", "Applied 'Reject' profile"],
    }
    base.update(over)
    return tuple(base[name] for name in at._RECORD_COLUMNS)


ERROR_TEXT = [("206", "Authentication failure", "Access denied by policy")]


class FakeConn:
    def __init__(self, db: FakeDb, kw: dict) -> None:
        self.db, self.kw, self.closed = db, kw, False

    def run(self, sql, **params):
        self.db.calls.append((self.kw["database"], sql, params))
        if "set_config" in sql:
            return [["15000", "on"]]
        if self.kw["database"] == at.CONFIG_DATABASE:
            if self.db.codes_error:
                raise self.db.codes_error
            return self.db.codes
        if self.db.records_error:
            raise self.db.records_error
        return self.db.records

    def close(self):
        self.closed = True


class FakeDb:
    """Stands in for ``pg8000.native.Connection``: records every connect and every statement."""

    def __init__(self, records=(), codes=ERROR_TEXT, *, refuse_tls=(), connect_error=None) -> None:
        self.records, self.codes = list(records), list(codes)
        self.refuse_tls = set(refuse_tls)
        self.connect_error = connect_error
        self.records_error: Exception | None = None
        self.codes_error: Exception | None = None
        self.connects: list[dict] = []
        self.calls: list[tuple] = []
        self.conns: list[FakeConn] = []

    def __call__(self, **kw):
        self.connects.append(kw)
        if self.connect_error is not None and kw["database"] in self.connect_error:
            raise self.connect_error[kw["database"]]
        if kw["ssl_context"] is not False and kw["port"] in self.refuse_tls:
            raise InterfaceError("Server refuses SSL")
        conn = FakeConn(self, kw)
        self.conns.append(conn)
        return conn

    def sql(self, database=at.LOGS_DATABASE):
        return [(sql, params) for db, sql, params in self.calls if db == database and "set_config" not in sql]


LOGIN = at.DbLogin(host="192.0.2.2", password=PASSWORD)


def read(db, **kw):
    return at.read_access_tracker(LOGIN, connect=db, now=NOW, **kw)


# ── the login ──────────────────────────────────────────────────────────────


def test_login_defaults_from_the_base_url():
    login, missing = at.db_login_from(password=PASSWORD, base_url="https://198.51.100.20/")
    assert missing == []
    assert (login.host, login.user, login.logs_port, login.config_port) == ("198.51.100.20", "appexternal", 5433, 5432)
    assert PASSWORD not in repr(login)


def test_login_overrides_and_problems():
    login, _ = at.db_login_from(
        password=PASSWORD, host="198.51.100.30", base_url="https://198.51.100.20", user="reader", logs_port="15433"
    )
    assert (login.host, login.user, login.logs_port) == ("198.51.100.30", "reader", 15433)
    assert at.db_login_from(password=None, base_url="https://198.51.100.20") == (None, ["CLEARPASS_DB_PASSWORD"])
    login, missing = at.db_login_from(password=PASSWORD)
    assert login is None and missing == ["CLEARPASS_DB_HOST (or CLEARPASS_BASE_URL)"]
    login, missing = at.db_login_from(password=PASSWORD, host="198.51.100.30", config_port="x")
    assert login is None and missing == ["CLEARPASS_DB_CONFIG_PORT (a port number, 1-65535)"]


def test_client_reads_the_database_login_from_its_variables():
    logins = {"CLEARPASS_BASE_URL": "https://198.51.100.20", "CLEARPASS_DB_PASSWORD": PASSWORD}
    client = ClearPassClient.from_env(Gate(read_only=True), logins=logins)
    assert client.db_login is not None and client.db_login.host == "198.51.100.20"
    assert client.has_login is False  # the API login is separate
    assert ClearPassClient.from_env(Gate(read_only=True), logins={}).db_login is None


# ── the queries ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mac", [MAC, "02-00-00-00-00-01", "0200.0000.0001", NORMALIZED, "02:00:00:00:00:01".upper()])
async def test_mac_formats_are_normalised(mac):
    db = FakeDb([row()])
    args = tools._tracker_args(mac, None, None, None)
    read(db, **args)
    ((sql, params),) = db.sql()
    assert params["mac"] == NORMALIZED
    assert NORMALIZED not in sql and ":mac" in sql


def test_every_value_is_a_parameter_never_sql_text():
    hostile = "x' OR '1'='1; DROP TABLE tips_alerts; --"
    db = FakeDb([row()])
    read(db, username=hostile, status="REJECT", service=hostile, hours=5, limit=7)
    ((sql, params),) = db.sql()
    assert hostile not in sql and "REJECT" not in sql.split("WHERE", 1)[1]
    assert params == {
        "username": hostile,
        "status": "REJECT",
        "service": hostile,
        "since": datetime(2025, 1, 2, 7, 0, tzinfo=UTC),
        "row_limit": 7,
    }
    assert sql.lstrip().upper().startswith("SELECT")
    for statement in [s for _, s, _ in db.calls]:
        assert statement.lstrip().upper().startswith("SELECT"), statement
        for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT"):
            assert word not in statement.upper().replace("UPDATED_AT", ""), (word, statement)
    codes_sql, codes_params = db.sql(at.CONFIG_DATABASE)[0]
    assert codes_params == {"codes": ["206"]} and "206" not in codes_sql


def test_sessions_are_read_only_with_a_statement_timeout_and_closed():
    db = FakeDb([row()])
    read(db, mac=NORMALIZED)
    setup = [(db_name, params) for db_name, sql, params in db.calls if "set_config" in sql]
    assert setup == [
        (at.LOGS_DATABASE, {"statement_timeout": "15000"}),
        (at.CONFIG_DATABASE, {"statement_timeout": "15000"}),
    ]
    assert all("default_transaction_read_only" in sql for _, sql, _ in db.calls if "set_config" in sql)
    assert all(conn.closed for conn in db.conns)
    assert [(c["host"], c["port"], c["database"], c["user"]) for c in db.connects] == [
        ("192.0.2.2", 5433, "tipsLogDb", "appexternal"),
        ("192.0.2.2", 5432, "tipsdb", "appexternal"),
    ]
    assert all(c["timeout"] == at.CONNECT_TIMEOUT_S for c in db.connects)


@pytest.mark.parametrize(
    ("hours", "limit", "want_hours", "want_limit"),
    [(0, 0, 1, 1), (-5, -1, 1, 1), (10_000, 10_000, 720, 100), (48, 25, 48, 25)],
)
def test_hours_and_limit_are_clamped(hours, limit, want_hours, want_limit):
    db = FakeDb([])
    out = read(db, mac=NORMALIZED, hours=hours, limit=limit)
    ((_, params),) = db.sql()
    assert params["row_limit"] == want_limit and out["limit"] == want_limit
    assert out["hours"] == want_hours
    assert (NOW - params["since"]).total_seconds() == want_hours * 3600


def test_filter_clauses_follow_the_filters_given():
    assert ":mac" in at.records_sql({"mac": "x"}) and ":username" not in at.records_sql({"mac": "x"})
    sql = at.records_sql({"username": "u", "status": "REJECT", "service": "s"})
    assert all(f":{name}" in sql for name in ("username", "status", "service", "since", "row_limit"))
    assert "ORDER BY s.timestamp DESC" in sql


# ── the reply ──────────────────────────────────────────────────────────────


def test_records_are_joined_and_shaped_with_error_text():
    tls_alert = "EAP-TLS: fatal alert by server - certificate_expired\\nTLS session error"
    db = FakeDb(
        [
            row(),
            row(session_id="R2", error_code=215, alerts=[tls_alert], roles=None, enforcement_profiles=None),
            row(session_id="R3", login_status="ACCEPT", error_code="0", alerts=None, monitor_mode="1"),
        ],
        codes=[*ERROR_TEXT, ("215", "Authentication failure", "TLS session error")],
    )
    out = read(db, mac=NORMALIZED)
    first, second, third = out["records"]
    assert first == {
        "time": "2025-01-02T11:30:00+00:00",
        "login_status": "REJECT",
        "source": "RADIUS",
        "service": "Corp 802.1X",
        "user": "test-user",
        "mac": MAC,
        "nad": {"name": "nad-1", "ip": "192.0.2.10", "port": "12"},
        "auth_type": "EAP-PEAP",
        "roles": ["[Employee]", "[Other]", "[User Authenticated]"],
        "enforcement_profiles": ["[Deny Access Profile]"],
        "error": {"code": 206, "group": "Authentication failure", "text": "Access denied by policy"},
        "alerts": ["Applied 'Reject' profile"],
        "session_id": "R00000001-01-00000001",
    }
    assert second["error"] == {"code": 215, "group": "Authentication failure", "text": "TLS session error"}
    assert second["alerts"] == ["EAP-TLS: fatal alert by server - certificate_expired", "TLS session error"]
    assert second["roles"] == [] and second["enforcement_profiles"] == []
    assert third["error"] is None and third["alerts"] == [] and third["monitor_mode"] == "1"
    assert out["error_codes"] == "resolved" and out["count"] == 3 and out["transport"] == "tls"
    assert out["filter"] == {"mac": MAC}
    assert sorted(db.sql(at.CONFIG_DATABASE)[0][1]["codes"]) == ["206", "215"]


def test_no_error_codes_means_no_second_database():
    db = FakeDb([row(login_status="ACCEPT", error_code=0)])
    out = read(db, mac=NORMALIZED)
    assert [c["database"] for c in db.connects] == [at.LOGS_DATABASE]
    assert "error_codes" not in out


@pytest.mark.parametrize(
    "failure",
    [
        DatabaseError({"S": "FATAL", "C": "28000", "M": "no pg_hba.conf entry"}),
        OSError("Connection refused"),
        InterfaceError("Server refuses SSL"),
    ],
)
def test_error_code_lookup_failure_keeps_the_numbers(failure):
    db = FakeDb([row()], connect_error={at.CONFIG_DATABASE: failure})
    out = read(db, mac=NORMALIZED)
    assert out["records"][0]["error"] == {"code": 206}
    assert out["error_codes"] == "unavailable"
    assert "numbers only" in out["error_codes_note"]


def test_empty_and_full_pages_say_so():
    assert "No Access Tracker records" in read(FakeDb([]), mac=NORMALIZED)["message"]
    out = read(FakeDb([row()] * 2), mac=NORMALIZED, limit=2)
    assert "newest 2" in out["message"]


# ── TLS first, plain only when the server refuses TLS ─────────────────────


def test_tls_is_tried_first_and_kept():
    db = FakeDb([row()])
    read(db, mac=NORMALIZED)
    assert all(c["ssl_context"] not in (None, False) for c in db.connects)


def test_plain_only_when_the_server_refuses_tls():
    db = FakeDb([row()], refuse_tls={5433})
    out = read(db, mac=NORMALIZED)
    assert out["transport"] == "plain"
    logs = [c for c in db.connects if c["database"] == at.LOGS_DATABASE]
    assert [c["ssl_context"] is False for c in logs] == [False, True]
    config = [c for c in db.connects if c["database"] == at.CONFIG_DATABASE]
    assert len(config) == 1 and config[0]["ssl_context"] is not False


@pytest.mark.parametrize(
    "failure",
    [OSError("handshake failed"), DatabaseError({"C": "28P01", "M": "password authentication failed"})],
)
def test_other_connect_failures_are_not_retried_in_plain(failure):
    db = FakeDb([row()], connect_error={at.LOGS_DATABASE: failure})
    out = read(db, mac=NORMALIZED)
    assert len(db.connects) == 1 and "error" in out


# ── failures and the password ─────────────────────────────────────────────


def test_rejected_login_says_where_to_set_the_password():
    failure = DatabaseError({"C": "28P01", "M": f'password authentication failed for "{PASSWORD}"'})
    out = read(FakeDb(connect_error={at.LOGS_DATABASE: failure}), mac=NORMALIZED)
    assert out["code"] == "login_rejected"
    assert "CLEARPASS_DB_PASSWORD" in out["guidance"] and "Server Configuration" in out["guidance"]
    assert PASSWORD not in json.dumps(out)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (OSError(f"timed out reaching {PASSWORD}"), "connection_error"),
        (TimeoutError("timed out"), "connection_error"),
        (DatabaseError({"C": "57014", "M": "canceling statement due to statement timeout"}), "timeout"),
        (DatabaseError({"C": "42P01", "M": f"relation {PASSWORD} does not exist"}), "database_error"),
        (InterfaceError(f"network error {PASSWORD}"), "connection_error"),
    ],
)
def test_failures_are_replies_and_never_hold_the_password(failure, code):
    db = FakeDb([row()])
    db.records_error = failure
    out = read(db, mac=NORMALIZED)
    assert out["code"] == code and "error" in out
    assert PASSWORD not in json.dumps(out)
    assert all(conn.closed for conn in db.conns)


# ── the tool ───────────────────────────────────────────────────────────────


def _backend(recording_transport, clearpass_client_factory, **db):
    client = clearpass_client_factory(gate=Gate(read_only=True), transport=recording_transport, **db)
    return Backend(tools.backend(client=lambda: client))


async def test_not_configured_says_what_to_set(recording_transport, clearpass_client_factory):
    backend = _backend(recording_transport, clearpass_client_factory)
    out = await backend.call("clearpass_access_tracker", {"mac": MAC})
    assert out["error"] == "login_missing" and out["product"] == "clearpass" and out["login"] == "access_tracker"
    assert out["missing"] == ["CLEARPASS_DB_PASSWORD"]
    for words in ("CLEARPASS_DB_PASSWORD", "appexternal", "Administration > Server Manager > Server Configuration"):
        assert words in out["guidance"]
    assert recording_transport.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"mac": MAC, "username": "u"},
        {"username": " "},
        {"mac": "02:00:00:00:00"},
        {"mac": MAC, "status": "DENIED"},
        {"mac": MAC, "service": " "},
    ],
)
async def test_bad_arguments_are_refused_before_connecting(recording_transport, clearpass_client_factory, args):
    backend = _backend(recording_transport, clearpass_client_factory, db_password=PASSWORD)
    out = await backend.call("clearpass_access_tracker", args)
    assert "error" in out and out.get("error") != "login_missing"


async def test_tool_reads_off_the_event_loop_and_hides_the_password(
    monkeypatch, recording_transport, clearpass_client_factory
):
    db = FakeDb([row()])
    monkeypatch.setattr(at, "_pg8000_connect", db)
    backend = _backend(recording_transport, clearpass_client_factory, db_password=PASSWORD)
    out = await backend.call("clearpass_access_tracker", {"mac": "02-00-00-00-00-01", "status": "reject"})
    assert out["records"][0]["error"]["text"] == "Access denied by policy"
    ((_, params),) = db.sql()
    assert params["status"] == "REJECT" and params["mac"] == NORMALIZED and params["row_limit"] == 10
    assert db.connects[0]["password"] == PASSWORD
    assert PASSWORD not in json.dumps(out)
    assert recording_transport.calls == []  # no API call


def test_registered_read_and_found_for_reject_reasons(recording_transport, clearpass_client_factory):
    assert labels("clearpass")["clearpass_access_tracker"]["kind"] == "read"
    props = _backend(recording_transport, clearpass_client_factory).schema("clearpass_access_tracker")["properties"]
    assert set(props) == {"mac", "username", "hours", "limit", "status", "service"}
    for question in (
        "clearpass access tracker",
        "why was this client rejected",
        "clearpass reject reason for this mac",
        "auth failure reason",
    ):
        names = [hit["name"] for hit in find.find_tool(question, top_k=3)]
        assert "clearpass_access_tracker" in names, (question, names)


# ── clearpass_why_denied with the database login ──────────────────────────


def _session():
    return {
        "id": "session-1",
        "username": "test-user",
        "mac_address": NORMALIZED,
        "acctstarttime": "2025-01-02T12:00:00Z",
    }


async def test_why_denied_adds_access_tracker_records(monkeypatch, recording_transport, clearpass_client_factory):
    db = FakeDb([row()])
    monkeypatch.setattr(at, "_pg8000_connect", db)
    recording_transport.reply({"_embedded": {"items": [_session()]}}, "GET", "/api/session")
    recording_transport.reply({"status": "Known"}, "GET", f"/api/endpoint/mac-address/{NORMALIZED}")
    backend = _backend(recording_transport, clearpass_client_factory, db_password=PASSWORD)
    out = await backend.call("clearpass_why_denied", {"mac": MAC})
    assert out["access_tracker"]["records"][0]["error"]["text"] == "Access denied by policy"
    assert "access_tracker" in out["limitation"] and out["degraded"] is False
    assert len(out["sessions"]) == 1
    assert PASSWORD not in json.dumps(out)


async def test_why_denied_answers_from_access_tracker_when_the_api_fails(
    monkeypatch, recording_transport, clearpass_client_factory
):
    monkeypatch.setattr(at, "_pg8000_connect", FakeDb([row()]))
    recording_transport.reply((403, {"detail": "Forbidden"}), "GET", "/api/session")
    recording_transport.reply({"status": "Known"}, "GET", f"/api/endpoint/mac-address/{NORMALIZED}")
    backend = _backend(recording_transport, clearpass_client_factory, db_password=PASSWORD)
    out = await backend.call("clearpass_why_denied", {"mac": MAC})
    assert out["sessions"] == [] and "error" in out["sessions_error"]
    assert out["access_tracker"]["records"][0]["login_status"] == "REJECT"
    assert out["degraded"] is True


async def test_why_denied_without_the_database_login_points_to_the_new_tool(
    recording_transport, clearpass_client_factory
):
    recording_transport.reply({"_embedded": {"items": []}}, "GET", "/api/session")
    out = await _backend(recording_transport, clearpass_client_factory).call("clearpass_why_denied", {"mac": MAC})
    assert "access_tracker" not in out
    assert "clearpass_access_tracker" in out["limitation"] and "CLEARPASS_DB_PASSWORD" in out["limitation"]


# ── access_check ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("extra", "want"), [({}, "missing"), ({"CLEARPASS_DB_PASSWORD": PASSWORD}, "set")])
async def test_access_check_reports_the_access_tracker_login(server_with_replies, extra, want):
    s = server_with_replies(
        {
            ("GET", "/api/oauth/me"): {"info": "API operator"},
            ("GET", "/api/oauth/privileges"): {"privileges": ["#cppm_config"]},
        },
        environ={"CLEARPASS_BASE_URL": "https://198.51.100.20", "CLEARPASS_API_TOKEN": "t", **extra},
    )
    out = await s.call("access_check", {})
    cp = next(p for p in out["products"] if p["product"] == "clearpass")
    assert cp["access_tracker"] == want and cp["access"] == "read-only"
    assert PASSWORD not in json.dumps(out)
