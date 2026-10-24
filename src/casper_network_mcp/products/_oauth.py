"""OAuth client credentials (a form POST) with the token kept in memory and renewed before it runs out.

UXI (through GreenLake) and OpsRamp log in this way. Central has its own copy, with a sync path.
A 401 (or OpsRamp's 407) drops the token; a plain read is then sent once more with a new one,
a change never is.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from casper_network_mcp.core.gate import PRODUCT_NAMES, is_write
from casper_network_mcp.core.http import Http
from casper_network_mcp.products._base import ApiError

__all__ = ["GREENLAKE_TOKEN_URL", "ClientCredentials", "send_bearer", "with_query"]

#: GreenLake issues the tokens for Central and UXI.
GREENLAKE_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
_EARLY_REFRESH_SECONDS = 60
_RENEW_STATUSES = (401, 407)


class ClientCredentials:
    """One API client's token, in memory only."""

    def __init__(self, product: str, http: Http, token_url: str, client_id: str, client_secret: str) -> None:
        self.product = product
        self.token_url = token_url
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    async def token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
        form = {"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret}
        resp = await self._http.request("POST", self.token_url, headers={"Accept": "application/json"}, data=form)
        return self._store(resp)

    def _store(self, resp: httpx.Response) -> str:
        name = PRODUCT_NAMES.get(self.product, self.product)
        if resp.status_code >= 400:
            raise ApiError(self.product, resp.status_code, f"the {name} login was refused", url=self.token_url)
        try:
            data = resp.json()
            token = str(data["access_token"] or "")
            lifetime = float(data.get("expires_in") or 3600)
            if not token:
                raise KeyError("access_token")
        except (ValueError, KeyError, TypeError):
            raise ApiError(self.product, resp.status_code, f"the {name} login reply had no token", url=self.token_url)
        with self._lock:
            self._token = token
            self._expires = time.monotonic() + max(0.0, lifetime - _EARLY_REFRESH_SECONDS)
        return token

    def forget(self, token: str) -> None:
        with self._lock:
            if self._token == token:
                self._token, self._expires = "", 0.0

    async def aclose(self) -> None:
        await self._http.aclose()


async def send_bearer(
    http: Http,
    creds: ClientCredentials,
    base_url: str,
    method: str,
    url: str,
    headers: dict[str, str],
    params: Any,
    body: dict[str, Any],
) -> httpx.Response:
    """Send with the bearer token; on 401/407 drop it and resend a plain read once."""
    token = await creds.token()
    resp = await http.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"}, params=params, **body)
    if resp.status_code in _RENEW_STATUSES:
        creds.forget(token)
        path = url.removeprefix(base_url).split("?", 1)[0]
        if method == "GET" and not body and not is_write(method, path):
            token = await creds.token()
            resp = await http.request(
                method, url, headers={**headers, "Authorization": f"Bearer {token}"}, params=params, **body
            )
    return resp


def with_query(url: str, params: Any) -> str:
    """``url`` with ``params`` encoded so a space is %20 and ``:`` and ``,`` stay as they are.

    OpsRamp reads ``+`` in a ``queryString`` as its field separator, so the usual ``+`` for a
    space would split one value into two filters.
    """
    if not params:
        return url
    clean = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in dict(params).items() if v is not None}
    return f"{url}?{urlencode(clean, quote_via=quote, safe=':,')}" if clean else url
