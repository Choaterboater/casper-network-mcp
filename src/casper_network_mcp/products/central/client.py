"""The Central client: OAuth client credentials, one gated request path, async and sync.

Adapted from hpe-networking-mcp ``pipeline/clients/token_manager.py`` and
``central_client.py``. What changed:
the login is ``CENTRAL_BASE_URL``, ``CENTRAL_CLIENT_ID`` and
``CENTRAL_CLIENT_SECRET`` from the process environment (no credentials file,
no env files); the token lives in memory only (never written to disk); the
source's write gate and its env switches are gone, replaced by the shared
gate every product uses.

Most of the copied Central tools are synchronous, so besides ``request()``
there is ``request_sync()``: a thin sync front door over the **same** gate,
path check, login and error handling, sending with ``httpx.Client`` on the
same transport settings. The SDK runs sync tools off the event loop.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.http import RETRYABLE_STATUSES, Http, body_kwargs, parse_retry_after
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused, Reply

__all__ = ["CENTRAL_PREFIXES", "TOKEN_URL", "ApiError", "CentralClient", "LoginMissing", "Refused", "Reply"]

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
#: The six API families in the bundled Central documents (a test keeps this in step with them).
CENTRAL_PREFIXES = (
    "/network-config/",
    "/network-monitoring/",
    "/network-notifications/",
    "/network-reporting/",
    "/network-services/",
    "/network-troubleshooting/",
)
_EARLY_REFRESH_SECONDS = 60
_SYNC_RETRIES = 2


class CentralClient(BaseClient):
    product = "central"
    prefixes = CENTRAL_PREFIXES
    probe_path = "/network-reporting/v1/reports"

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None,
        client_id: str | None,
        client_secret: str | None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        token_url: str = TOKEN_URL,
    ) -> None:
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._token_url = token_url
        self._token = ""
        self._token_expires = 0.0
        self._token_lock = threading.Lock()
        super().__init__(
            gate=gate,
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            has_login=bool(client_id and client_secret),
        )
        self._login_http = Http("central-login", timeout=timeout, transport=transport)  # type: ignore[arg-type]
        self._sync_lock = threading.Lock()
        self._sync_client: httpx.Client | None = None

    @classmethod
    def from_env(cls, gate: Gate, logins: Mapping[str, str] | None = None, **kw: Any) -> CentralClient:
        """A client from the login variables (``logins``, or read from the environment)."""
        logins = read_logins() if logins is None else logins
        return cls(
            gate=gate,
            base_url=logins.get("CENTRAL_BASE_URL"),
            client_id=logins.get("CENTRAL_CLIENT_ID"),
            client_secret=logins.get("CENTRAL_CLIENT_SECRET"),
            **kw,
        )

    # ── the token (memory only) ─────────────────────────────────────────────

    def _form(self) -> dict[str, str]:
        return {"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret}

    def _cached_token(self) -> str | None:
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires:
                return self._token
            return None

    def _store_token(self, resp: httpx.Response) -> str:
        if resp.status_code >= 400:
            raise ApiError("central", resp.status_code, "the Central login was refused", url=self._token_url)
        try:
            data = resp.json()
            token = str(data["access_token"])
            lifetime = float(data.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError):
            raise ApiError("central", resp.status_code, "the Central login reply had no token", url=self._token_url)
        with self._token_lock:
            self._token = token
            self._token_expires = time.monotonic() + max(0.0, lifetime - _EARLY_REFRESH_SECONDS)
        return token

    def _forget_token(self, token: str) -> None:
        with self._token_lock:
            if self._token == token:
                self._token, self._token_expires = "", 0.0

    async def _access_token(self) -> str:
        cached = self._cached_token()
        if cached:
            return cached
        resp = await self._login_http.request(
            "POST", self._token_url, headers={"Accept": "application/json"}, data=self._form()
        )
        return self._store_token(resp)

    def _access_token_sync(self) -> str:
        cached = self._cached_token()
        if cached:
            return cached
        resp = self._sync().post(self._token_url, data=self._form(), headers={"Accept": "application/json"})
        return self._store_token(resp)

    # ── async front door ────────────────────────────────────────────────────

    async def _send(self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]) -> Any:
        token = await self._access_token()
        resp = await self._http.request(
            method, url, headers={**headers, "Authorization": f"Bearer {token}"}, params=params, **body
        )
        if resp.status_code == 401:  # not accepted, so sending again is safe
            self._forget_token(token)
            token = await self._access_token()
            resp = await self._http.request(
                method, url, headers={**headers, "Authorization": f"Bearer {token}"}, params=params, **body
            )
        return resp

    # ── sync front door (same gate, same checks) ───────────────────────────

    def _sync(self) -> httpx.Client:
        with self._sync_lock:
            if self._sync_client is None or self._sync_client.is_closed:
                transport = self._transport if isinstance(self._transport, httpx.BaseTransport) else None
                self._sync_client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout, connect=10.0),
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                    transport=transport,
                )
            return self._sync_client

    def _send_sync_once(
        self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]
    ) -> httpx.Response:
        token = self._access_token_sync()
        resp = self._sync().request(
            method, url, headers={**headers, "Authorization": f"Bearer {token}"}, params=params, **body
        )
        if resp.status_code == 401:
            self._forget_token(token)
            token = self._access_token_sync()
            resp = self._sync().request(
                method, url, headers={**headers, "Authorization": f"Bearer {token}"}, params=params, **body
            )
        return resp

    def _roundtrip_sync(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: Any,
        kind: str | None,
        headers: dict[str, str] | None,
        content_type: str,
        path_args: dict[str, Any] | None,
    ) -> tuple[httpx.Response, str]:
        method = method.upper()
        url = self._prepare(method, path, kind=kind, path_args=path_args)
        body, body_headers = body_kwargs(json, content_type)
        sent_headers = self._headers(headers, body_headers)
        retries = _SYNC_RETRIES if method == "GET" and not body else 0
        delay = 0.5
        for attempt in range(retries + 1):
            resp = self._send_sync_once(method, url, sent_headers, params, body)
            if resp.status_code not in RETRYABLE_STATUSES or attempt == retries:
                break
            hint = parse_retry_after(resp.headers.get("Retry-After", ""))
            time.sleep(min(hint if hint is not None else delay * random.uniform(0.8, 1.2), 8.0))
            delay *= 2
        return resp, url

    def request_sync(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        kind: str | None = None,
        headers: dict[str, str] | None = None,
        content_type: str = "application/json",
        path_args: dict[str, Any] | None = None,
    ) -> Any:
        """The sync twin of :meth:`request`: gate, path check, login, send, hide secrets."""
        resp, url = self._roundtrip_sync(
            method,
            path,
            params=params,
            json=json,
            kind=kind,
            headers=headers,
            content_type=content_type,
            path_args=path_args,
        )
        return self._decode(resp, method.upper(), url)

    def exchange_sync(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        kind: str | None = None,
        headers: dict[str, str] | None = None,
        content_type: str = "application/json",
        path_args: dict[str, Any] | None = None,
    ) -> Reply:
        """The sync twin of :meth:`exchange`: same gate and checks, error statuses come back as a ``Reply``."""
        resp, url = self._roundtrip_sync(
            method,
            path,
            params=params,
            json=json,
            kind=kind,
            headers=headers,
            content_type=content_type,
            path_args=path_args,
        )
        return self._reply(resp, url)

    async def aclose(self) -> None:
        await super().aclose()
        await self._login_http.aclose()
        with self._sync_lock:
            if self._sync_client is not None:
                self._sync_client.close()
                self._sync_client = None
