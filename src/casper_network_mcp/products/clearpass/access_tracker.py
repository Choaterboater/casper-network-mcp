"""ClearPass Access Tracker, read from ClearPass's own database as the read-only ``appexternal`` user.

ClearPass's REST and XML APIs do not expose Access Tracker: ``/api/session`` holds only active
accounting sessions, so a reject and its reason can't be read there. ClearPass does let an
outside reader into its Postgres as ``appexternal`` (read-only; the admin sets its password in
ClearPass under Administration > Server Manager > Server Configuration > Change Password):

* the logs database ``tipsLogDb`` (port 5433): ``tips_dashboard_summary`` (the Access Tracker
  list), ``tips_policy_server_session_log`` (auth type, roles, enforcement profiles) and
  ``tips_alerts``, joined on the session id;
* the config database ``tipsdb`` (port 5432, TLS only): ``tips_error_codes_view``, the text of
  each error code. Best effort: when it can't be read, records keep the numeric code.

Every statement is a fixed ``SELECT``; values (MAC, username, status, service, time, limit) go
in as parameters, never into the SQL text. Each call opens its own connections and closes them,
marks the session read-only and sets a statement timeout. TLS is tried first; a server that
refuses TLS is read over a plain connection and the reply says so. The server's certificate is
not checked (ClearPass's database certificate is its own), so TLS here protects against reading
on the wire, not against a host pretending to be ClearPass.

The password lives in :class:`DbLogin` only (hidden from its ``repr``) and is scrubbed from any
error text before it is returned.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from casper_network_mcp.core.redact import redact_tool_error_text
from casper_network_mcp.core.url_validation import placeholder_host_reason

__all__ = [
    "CONNECT_TIMEOUT_S",
    "DB_VARS",
    "STATEMENT_TIMEOUT_MS",
    "STATUSES",
    "DbLogin",
    "clamp_hours",
    "clamp_rows",
    "db_login_from",
    "not_configured",
    "read_access_tracker",
]

#: The login variables for the database login (all optional; the password turns it on).
DB_VARS = (
    "CLEARPASS_DB_PASSWORD",
    "CLEARPASS_DB_HOST",
    "CLEARPASS_DB_USER",
    "CLEARPASS_DB_LOGS_PORT",
    "CLEARPASS_DB_CONFIG_PORT",
)
DEFAULT_USER = "appexternal"
DEFAULT_LOGS_PORT = 5433
DEFAULT_CONFIG_PORT = 5432
LOGS_DATABASE = "tipsLogDb"
CONFIG_DATABASE = "tipsdb"
CONNECT_TIMEOUT_S = 10.0
STATEMENT_TIMEOUT_MS = 15000
STATUSES = ("ACCEPT", "REJECT", "TIMEOUT")
DEFAULT_ROWS, MAX_ROWS = 10, 100
DEFAULT_HOURS, MAX_HOURS = 24, 720

WHERE_TO_SET = (
    "ClearPass sets the password of its read-only database user appexternal under Administration > Server Manager > "
    "Server Configuration > Change Password (user appexternal)."
)


@dataclass(frozen=True)
class DbLogin:
    host: str
    password: str = field(repr=False)
    user: str = DEFAULT_USER
    logs_port: int = DEFAULT_LOGS_PORT
    config_port: int = DEFAULT_CONFIG_PORT


def _port(raw: str | None, default: int) -> int | None:
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if value.isdigit() and 0 < int(value) < 65536:
        return int(value)
    return None


def db_login_from(
    *,
    password: str | None,
    host: str | None = None,
    base_url: str | None = None,
    user: str | None = None,
    logs_port: str | None = None,
    config_port: str | None = None,
) -> tuple[DbLogin | None, list[str]]:
    """The database login, or ``None`` and what is missing or wrong (variable names, never values)."""
    problems: list[str] = []
    if not (password or "").strip():
        problems.append("CLEARPASS_DB_PASSWORD")
    db_host = (host or "").strip()
    if not db_host and base_url:
        try:
            db_host = urlsplit(base_url.strip() if "//" in base_url else f"https://{base_url.strip()}").hostname or ""
        except ValueError:
            db_host = ""
    if not db_host:
        problems.append("CLEARPASS_DB_HOST (or CLEARPASS_BASE_URL)")
    elif placeholder_host_reason(db_host):
        problems.append("CLEARPASS_DB_HOST (the host is a placeholder)")
    ports = (_port(logs_port, DEFAULT_LOGS_PORT), _port(config_port, DEFAULT_CONFIG_PORT))
    for name, port in zip(("CLEARPASS_DB_LOGS_PORT", "CLEARPASS_DB_CONFIG_PORT"), ports, strict=True):
        if port is None:
            problems.append(f"{name} (a port number, 1-65535)")
    if problems or ports[0] is None or ports[1] is None:
        return None, problems
    return (
        DbLogin(
            host=db_host,
            password=(password or "").strip(),
            user=(user or "").strip() or DEFAULT_USER,
            logs_port=ports[0],
            config_port=ports[1],
        ),
        [],
    )


def not_configured(missing: list[str]) -> dict[str, Any]:
    """The reply when the database login is not set: what to set, and where in ClearPass."""
    return {
        "error": "login_missing",
        "product": "clearpass",
        "login": "access_tracker",
        "missing": list(missing) or ["CLEARPASS_DB_PASSWORD"],
        "guidance": "Access Tracker is read from ClearPass's database, which needs its own login. Set "
        "CLEARPASS_DB_PASSWORD to the password of the read-only database user appexternal. " + WHERE_TO_SET + " "
        "Optional: CLEARPASS_DB_HOST (default: the host in CLEARPASS_BASE_URL), CLEARPASS_DB_USER (default "
        "appexternal), CLEARPASS_DB_LOGS_PORT (default 5433), CLEARPASS_DB_CONFIG_PORT (default 5432). Until then, "
        "open Monitoring > Live Monitoring > Access Tracker in the ClearPass UI for the reject reason.",
    }


def clamp_rows(limit: int | None) -> int:
    try:
        value = int(limit) if limit is not None else DEFAULT_ROWS
    except (TypeError, ValueError):
        value = DEFAULT_ROWS
    return max(1, min(MAX_ROWS, value))


def clamp_hours(hours: int | None) -> int:
    try:
        value = int(hours) if hours is not None else DEFAULT_HOURS
    except (TypeError, ValueError):
        value = DEFAULT_HOURS
    return max(1, min(MAX_HOURS, value))


# ── queries (fixed text; every value is a named parameter) ─────────────────

_SESSION_SETUP = (
    "SELECT set_config('statement_timeout', :statement_timeout, false), "
    "set_config('default_transaction_read_only', 'on', false)"
)

_RECORD_COLUMNS = (
    "session_id", "timestamp", "login_status", "source", "service", "user", "mac", "nas_name", "nas_ip",
    "nas_port", "error_code", "auth_type", "roles", "enforcement_profiles", "monitor_mode", "alerts",
)  # fmt: skip

_RECORDS_SELECT = """
SELECT s.id, s.timestamp, s.login_status, s.source, s.service_name, s.user_name, s.host_mac,
       s.nas_name, s.nas_ip, s.nas_port, s.error_code,
       p.auth_type, p.roles, p.enf_profiles, p.monitor_mode,
       (SELECT array_agg(DISTINCT a.alert) FROM tips_alerts a WHERE a.session_id = s.id) AS alerts
FROM tips_dashboard_summary s
LEFT JOIN tips_policy_server_session_log p ON p.id = s.id
WHERE s.timestamp >= :since"""

#: Filter clauses, picked by which filters were given; the clause text never holds a value.
_FILTERS = {
    "mac": " AND s.host_mac = :mac",
    "username": " AND s.user_name = :username",
    "status": " AND s.login_status = :status",
    "service": " AND s.service_name = :service",
}
_RECORDS_ORDER = " ORDER BY s.timestamp DESC LIMIT :row_limit"

_ERROR_CODES_SQL = (
    "SELECT error_code::text, error_group_str, error_code_str FROM tips_error_codes_view "
    "WHERE error_code::text = ANY(CAST(:codes AS text[]))"
)


def records_sql(filters: Mapping[str, Any]) -> str:
    """The records query for the filters given (``mac``, ``username``, ``status``, ``service``)."""
    clauses = "".join(clause for name, clause in _FILTERS.items() if filters.get(name) is not None)
    return _RECORDS_SELECT + clauses + _RECORDS_ORDER


# ── connections ─────────────────────────────────────────────────────────────

Connect = Callable[..., Any]


def _pg8000_connect(**kw: Any) -> Any:
    import pg8000.native  # type: ignore[import-untyped]

    return pg8000.native.Connection(**kw)


def _refuses_tls(exc: Exception) -> bool:
    from pg8000.exceptions import InterfaceError  # type: ignore[import-untyped]

    return isinstance(exc, InterfaceError) and "refuses ssl" in str(exc).lower()


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # ClearPass's database certificate is its own; see the module notes
    return context


def open_connection(login: DbLogin, port: int, database: str, connect: Connect | None = None) -> tuple[Any, str]:
    """A read-only connection and how it is carried (``"tls"`` or ``"plain"``).

    TLS first; only a server that answers "no TLS" to the TLS request gets a plain connection.
    Any other failure (refused login, no route, a TLS handshake error) is raised as is.
    """
    connect = connect or _pg8000_connect
    kw = {
        "user": login.user,
        "password": login.password,
        "host": login.host,
        "port": port,
        "database": database,
        "timeout": CONNECT_TIMEOUT_S,
        "application_name": "casper-network-mcp",
    }
    try:
        conn, transport = connect(ssl_context=_tls_context(), **kw), "tls"
    except Exception as exc:
        if not _refuses_tls(exc):
            raise
        conn, transport = connect(ssl_context=False, **kw), "plain"
    try:
        conn.run(_SESSION_SETUP, statement_timeout=str(STATEMENT_TIMEOUT_MS))
    except Exception:
        _close(conn)
        raise
    return conn, transport


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001, S110 - closing is best effort
        pass


# ── shaping ─────────────────────────────────────────────────────────────────


def _mac_text(value: Any) -> Any:
    if isinstance(value, str) and len(value) == 12 and all(c in "0123456789abcdefABCDEF" for c in value):
        low = value.lower()
        return ":".join(low[i : i + 2] for i in range(0, 12, 2))
    return value


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _alerts(value: Any) -> list[str]:
    lines: list[str] = []
    for alert in value or []:
        for line in str(alert).replace("\\n", "\n").splitlines():
            line = line.strip()
            if line and line not in lines:
                lines.append(line)
    return lines


def _code(value: Any) -> int | str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else text


def _time(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


_MONITOR_OFF = {"", "0", "false", "f", "off", "disabled", "no"}


def shape_record(row: Any, error_texts: Mapping[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    raw = dict(zip(_RECORD_COLUMNS, row, strict=False))
    code = _code(raw.get("error_code"))
    nad = (("name", raw.get("nas_name")), ("ip", raw.get("nas_ip")), ("port", raw.get("nas_port")))
    record: dict[str, Any] = {
        "time": _time(raw.get("timestamp")),
        "login_status": raw.get("login_status"),
        "source": raw.get("source"),
        "service": raw.get("service"),
        "user": raw.get("user"),
        "mac": _mac_text(raw.get("mac")),
        "nad": {k: v for k, v in nad if v not in (None, "")},
        "auth_type": raw.get("auth_type") or None,
        "roles": _split_list(raw.get("roles")),
        "enforcement_profiles": _split_list(raw.get("enforcement_profiles")),
        "error": None,
        "alerts": _alerts(raw.get("alerts")),
        "session_id": raw.get("session_id"),
    }
    monitor = raw.get("monitor_mode")
    if monitor not in (None, False, 0) and str(monitor).strip().lower() not in _MONITOR_OFF:
        record["monitor_mode"] = monitor
    if code not in (None, 0):
        error: dict[str, Any] = {"code": code}
        text = (error_texts or {}).get(str(code))
        if text:
            error.update(text)
        record["error"] = error
    return record


# ── the read ────────────────────────────────────────────────────────────────


def _scrub(text: str, login: DbLogin) -> str:
    if login.password:
        text = text.replace(login.password, "[hidden]")
    return redact_tool_error_text(text)


def _failure(exc: Exception, login: DbLogin, what: str) -> dict[str, Any]:
    """A database failure as ``{"error", "code", "guidance"}``; the password never appears."""
    from pg8000.exceptions import DatabaseError  # type: ignore[import-untyped]

    if isinstance(exc, DatabaseError) and exc.args and isinstance(exc.args[0], dict):
        info = exc.args[0]
        sqlstate, message = str(info.get("C", "")), str(info.get("M", ""))
        if sqlstate == "28P01":
            return {
                "error": _scrub(f"ClearPass's database turned the {login.user} login away ({what}).", login),
                "code": "login_rejected",
                "guidance": "Check CLEARPASS_DB_PASSWORD (and CLEARPASS_DB_USER). " + WHERE_TO_SET,
            }
        if sqlstate == "57014":
            return {
                "error": f"The {what} query ran past {STATEMENT_TIMEOUT_MS // 1000} s and was stopped.",
                "code": "timeout",
                "guidance": "Ask for fewer hours or a narrower filter.",
            }
        return {
            "error": _scrub(f"ClearPass's database refused the {what} read: {message} ({sqlstate})", login),
            "code": "database_error",
        }
    if isinstance(exc, (TimeoutError, OSError)):
        return {
            "error": _scrub(f"Could not reach ClearPass's database for the {what}: {exc}", login),
            "code": "connection_error",
            "guidance": f"The database port must be reachable from this machine ({login.host}, ports "
            f"{login.logs_port} and {login.config_port}); a firewall between them is the usual cause.",
        }
    return {
        "error": _scrub(f"Could not read the {what} from ClearPass's database: {type(exc).__name__}: {exc}", login),
        "code": "connection_error",
    }


def _error_texts(login: DbLogin, codes: list[str], connect: Connect | None) -> dict[str, dict[str, Any]]:
    conn, _ = open_connection(login, login.config_port, CONFIG_DATABASE, connect)
    try:
        rows = conn.run(_ERROR_CODES_SQL, codes=codes) or []
    finally:
        _close(conn)
    out: dict[str, dict[str, Any]] = {}
    for code, group, text in rows:
        out[str(code)] = {k: v for k, v in (("group", group), ("text", text)) if v}
    return out


def read_access_tracker(
    login: DbLogin,
    *,
    mac: str | None = None,
    username: str | None = None,
    status: str | None = None,
    service: str | None = None,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_ROWS,
    connect: Connect | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Access Tracker records, newest first (blocking; run it off the event loop).

    ``mac`` must already be 12 lowercase hex digits; ``status`` one of :data:`STATUSES`.
    """
    safe_hours, safe_limit = clamp_hours(hours), clamp_rows(limit)
    since = (now or datetime.now(UTC)) - timedelta(hours=safe_hours)
    filters = {"mac": mac, "username": username, "status": status, "service": service}
    params: dict[str, Any] = {k: v for k, v in filters.items() if v is not None}
    params.update(since=since, row_limit=safe_limit)
    shown_filter = {k: (_mac_text(v) if k == "mac" else v) for k, v in filters.items() if v is not None}
    try:
        conn, transport = open_connection(login, login.logs_port, LOGS_DATABASE, connect)
        try:
            rows = conn.run(records_sql(filters), **params) or []
        finally:
            _close(conn)
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error reply, never a trace
        return {**_failure(exc, login, "Access Tracker records"), "filter": shown_filter}
    out: dict[str, Any] = {
        "source": f"ClearPass Access Tracker ({LOGS_DATABASE}, read-only {login.user})",
        "filter": shown_filter,
        "hours": safe_hours,
        "limit": safe_limit,
        "transport": transport,
    }
    error_index = _RECORD_COLUMNS.index("error_code")
    codes = sorted(
        {str(c) for c in (_code(r[error_index]) for r in rows if len(r) > error_index) if c not in (None, 0)}
    )
    texts: dict[str, dict[str, Any]] = {}
    if codes:
        try:
            texts = _error_texts(login, codes, connect)
            out["error_codes"] = "resolved"
        except Exception as exc:  # noqa: BLE001 - best effort: keep the numeric codes
            out["error_codes"] = "unavailable"
            out["error_codes_note"] = (
                "Error codes are numbers only: their text (tips_error_codes_view in tipsdb, port "
                f"{login.config_port}) could not be read. {_failure(exc, login, 'error-code text')['error']}"
            )
    out["records"] = [shape_record(row, texts) for row in rows]
    out["count"] = len(out["records"])
    if not out["records"]:
        out["message"] = f"No Access Tracker records match in the last {safe_hours} hours."
    elif len(out["records"]) >= safe_limit:
        out["message"] = f"Showing the newest {safe_limit}; there may be more (raise limit, up to {MAX_ROWS})."
    return out
