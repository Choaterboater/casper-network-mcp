"""The ClearPass client: a bearer token against ``CLEARPASS_BASE_URL``.

The login is either a pasted ``CLEARPASS_API_TOKEN`` (sent as is; it expires
when ClearPass says) or an API client, ``CLEARPASS_CLIENT_ID`` and
``CLEARPASS_CLIENT_SECRET``, which logs in by itself at ``POST /api/oauth``
(client credentials, JSON body) and renews its token before it runs out. A
pasted token wins when both are set. The fetched token lives in memory only.

ClearPass is usually on a private address on site; that is allowed (the
login is the limit). Its API lives under ``/api``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.http import Http
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused

__all__ = ["TOKEN_PATH", "ApiError", "ClearPassClient", "LoginMissing", "Refused"]

TOKEN_PATH = "/api/oauth"
_EARLY_REFRESH_SECONDS = 60


class ClearPassClient(BaseClient):
    product = "clearpass"
    prefixes = ("/api/",)
    probe_path = "/api/oauth/me"

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None,
        token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_token = token or ""
        # A pasted token wins; the API client is used only without one.
        self._client_id = "" if token else (client_id or "")
        self._client_secret = "" if token else (client_secret or "")
        self._token = ""
        self._token_expires = 0.0
        self._token_lock = threading.Lock()
        super().__init__(
            gate=gate,
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            has_login=bool(token or (client_id and client_secret)),
        )
        self._login_http = Http("clearpass-login", timeout=timeout, transport=transport)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, gate: Gate, logins: Mapping[str, str] | None = None, **kw: Any) -> ClearPassClient:
        """A client from the login variables (``logins``, or read from the environment)."""
        logins = read_logins() if logins is None else logins
        return cls(
            gate=gate,
            base_url=logins.get("CLEARPASS_BASE_URL"),
            token=logins.get("CLEARPASS_API_TOKEN"),
            client_id=logins.get("CLEARPASS_CLIENT_ID"),
            client_secret=logins.get("CLEARPASS_CLIENT_SECRET"),
            **kw,
        )

    def _login_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}"}

    # ── the API client's token (memory only) ────────────────────────────────

    @property
    def _token_url(self) -> str:
        return f"{self.base_url}{TOKEN_PATH}"

    def _cached_token(self) -> str | None:
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires:
                return self._token
            return None

    def _store_token(self, resp: httpx.Response) -> str:
        if resp.status_code >= 400:
            raise ApiError("clearpass", resp.status_code, "the ClearPass login was refused", url=self._token_url)
        try:
            data = resp.json()
            token = str(data["access_token"] or "")
            lifetime = float(data.get("expires_in") or 3600)
            if not token:
                raise KeyError("access_token")
        except (ValueError, KeyError, TypeError):
            raise ApiError("clearpass", resp.status_code, "the ClearPass login reply had no token", url=self._token_url)
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
        body = {"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret}
        resp = await self._login_http.request(
            "POST", self._token_url, headers={"Accept": "application/json"}, json=body
        )
        return self._store_token(resp)

    # ── the async front door ────────────────────────────────────────────────

    async def _send(self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]) -> Any:
        if self._api_token:
            return await super()._send(method, url, headers, params, body)
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

    async def aclose(self) -> None:
        await super().aclose()
        await self._login_http.aclose()
