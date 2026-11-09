"""The AOS 8 client: a session login to a Mobility Conductor or Mobility Controller.

Adapted from hpe-networking-mcp ``mcp_servers/aos8.py`` (its session flow). The login is
``AOS8_BASE_URL`` (``https://<conductor>:4343``), ``AOS8_USERNAME`` and ``AOS8_PASSWORD``.

* ``POST /v1/api/login`` (form ``username`` and ``password``) answers
  ``_global_result.UIDARUBA``, a session id, and a ``SESSION`` cookie. Every request then
  carries ``UIDARUBA`` as a query value and the cookie, added last so no argument can
  replace them. Both live in memory only and never appear in a reply or an error.
* Controllers allow few API sessions, so one session is reused until it has been idle for
  ``SESSION_IDLE_SECONDS``; a read answered 401 logs in again once and is sent again.
* When the server stops, the session is ended with ``GET /v1/api/logout``.

The certificate is checked like every other product's; a controller with a private CA needs
that CA trusted (for example with ``SSL_CERT_FILE``). Only ``/v1/configuration/`` is reachable
through :meth:`request`, and the tools only ever send ``GET``.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate, is_write
from casper_network_mcp.core.http import Http, register_close_hook
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused, TransportFailure

__all__ = [
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "OBJECT_PATH",
    "SHOW_PATH",
    "Aos8Client",
    "ApiError",
    "LoginMissing",
    "Refused",
]

LOGIN_PATH = "/v1/api/login"
LOGOUT_PATH = "/v1/api/logout"
SHOW_PATH = "/v1/configuration/showcommand"
OBJECT_PATH = "/v1/configuration/object"
#: Reuse a session while it was used within this many seconds (AOS 8 idles API sessions out after about 15 minutes).
SESSION_IDLE_SECONDS = 600.0


@dataclass
class _Session:
    uid: str
    cookie: str | None
    last_used: float


_LIVE: weakref.WeakSet[Aos8Client] = weakref.WeakSet()


def _rename_auth_columns(data: Any) -> Any:
    """``data`` with each table row's ``Auth`` key renamed ``Auth method``; ``data`` itself when there is none."""
    if not isinstance(data, dict):
        return data
    changed = False
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, list) and any(isinstance(r, dict) and "Auth" in r for r in value):
            changed = True
            value = [
                {("Auth method" if k == "Auth" and isinstance(v, (str, type(None))) else k): v for k, v in r.items()}
                if isinstance(r, dict)
                else r
                for r in value
            ]
        out[key] = value
    return out if changed else data


async def _logout_all() -> None:
    for client in list(_LIVE):
        await client.logout()


class Aos8Client(BaseClient):
    product = "aos8"
    prefixes = ("/v1/configuration/",)
    probe_path = SHOW_PATH

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None,
        username: str | None,
        password: str | None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.username = username or ""
        self._password = password or ""
        self._session: _Session | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        super().__init__(
            gate=gate,
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            has_login=bool(username and password),
        )
        self._login_http = Http("aos8-login", timeout=timeout, transport=transport)  # type: ignore[arg-type]
        _LIVE.add(self)
        register_close_hook(_logout_all)

    @classmethod
    def from_env(cls, gate: Gate, logins: Mapping[str, str] | None = None, **kw: Any) -> Aos8Client:
        """A client from the login variables (``logins``, or read from the environment)."""
        logins = read_logins() if logins is None else logins
        return cls(
            gate=gate,
            base_url=logins.get("AOS8_BASE_URL"),
            username=logins.get("AOS8_USERNAME"),
            password=logins.get("AOS8_PASSWORD"),
            **kw,
        )

    def _login_headers(self) -> dict[str, str]:
        return {}

    def _decode(self, resp: httpx.Response, method: str, url: str) -> Any:
        """As the base, but a table's ``Auth`` column (the auth method, ``802.1x``) is kept readable.

        The secret filter hides any value under a key named ``auth``; in a showcommand table that
        column holds no secret, so it is renamed ``Auth method`` first.
        """
        if resp.status_code < 400 and resp.content:
            try:
                data = resp.json()
            except ValueError:
                data = None
            renamed = _rename_auth_columns(data)
            if renamed is not data:
                resp = httpx.Response(resp.status_code, json=renamed, headers={"content-type": "application/json"})
        return super()._decode(resp, method, url)

    # ── the session (memory only) ───────────────────────────────────────────

    def _lock(self) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in self._locks:
            self._locks = {loop_id: asyncio.Lock()}
        return self._locks[loop_id]

    def _live_session(self) -> _Session | None:
        session = self._session
        if session is not None and time.monotonic() - session.last_used < SESSION_IDLE_SECONDS:
            return session
        return None

    async def _login(self) -> _Session:
        url = f"{self.base_url}{LOGIN_PATH}"
        form = {"username": self.username, "password": self._password}
        try:
            resp = await self._login_http.request("POST", url, headers={"Accept": "application/json"}, data=form)
        except httpx.TransportError as exc:
            raise TransportFailure(self.product, exc, url=url, change=False) from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        result = data.get("_global_result") if isinstance(data, dict) else None
        uid = str(result.get("UIDARUBA") or "") if isinstance(result, dict) else ""
        if resp.status_code >= 500:
            raise ApiError(self.product, resp.status_code, "the AOS 8 login failed", url=url, method="GET")
        if resp.status_code >= 400 or not uid:
            # A refused login is reported like an expired one, so Casper asks for a new login.
            raise ApiError(self.product, 401, "the AOS 8 login was refused", url=url, method="GET")
        cookie = resp.cookies.get("SESSION")
        return _Session(uid=uid, cookie=f"SESSION={cookie}" if cookie else None, last_used=time.monotonic())

    async def _current(self) -> _Session:
        session = self._live_session()
        if session is not None:
            return session
        async with self._lock():
            session = self._live_session()
            if session is None:
                session = self._session = await self._login()
            return session

    def _forget(self, session: _Session) -> None:
        if self._session is session:
            self._session = None

    @staticmethod
    def _with_session(headers: dict[str, str], params: Any, session: _Session) -> tuple[dict[str, str], Any]:
        query: Any
        if isinstance(params, Mapping):
            query = {**params, "UIDARUBA": session.uid}
        else:
            query = [*(params or []), ("UIDARUBA", session.uid)]
        out = dict(headers)
        if session.cookie:
            out["Cookie"] = session.cookie
        return out, query

    @property
    def has_session(self) -> bool:
        """True while a session is held (the id itself is never shown)."""
        return self._session is not None

    # ── the async front door ────────────────────────────────────────────────

    async def _send(self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]) -> Any:
        session = await self._current()
        send_headers, query = self._with_session(headers, params, session)
        resp = await self._http.request(method, url, headers=send_headers, params=query, **body)
        if resp.status_code == 401:
            self._forget(session)
            if method == "GET" and not body and not is_write(method, url.removeprefix(self.base_url)):
                session = await self._current()
                send_headers, query = self._with_session(headers, params, session)
                resp = await self._http.request(method, url, headers=send_headers, params=query, **body)
        if resp.status_code < 400:
            session.last_used = time.monotonic()
        return resp

    async def logout(self) -> bool:
        """End the session on the controller (``GET /v1/api/logout``); True when one was held."""
        session, self._session = self._session, None
        if session is None or not self.base_url:
            return False
        headers, query = self._with_session({"Accept": "application/json"}, None, session)
        try:
            await self._login_http.request("GET", f"{self.base_url}{LOGOUT_PATH}", headers=headers, params=query)
        except httpx.HTTPError:
            pass  # the controller ends an idle session by itself
        return True

    async def aclose(self) -> None:
        await self.logout()
        await super().aclose()
        await self._login_http.aclose()
