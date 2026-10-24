"""The UXI client: a GreenLake personal API client made for the User Experience Insight service.

The login is ``UXI_CLIENT_ID`` and ``UXI_CLIENT_SECRET``; ``UXI_BASE_URL`` is optional (default
``https://api.capenetworks.com``, the host in HPE's published UXI document). The token comes from
GreenLake (client credentials, as for Central), lives in memory only and is renewed before it runs
out. Every path is under ``/networking-uxi/v1alpha1/``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.http import Http
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused
from casper_network_mcp.products._oauth import GREENLAKE_TOKEN_URL, ClientCredentials, send_bearer

__all__ = ["API", "DEFAULT_BASE_URL", "ApiError", "LoginMissing", "Refused", "UxiClient"]

DEFAULT_BASE_URL = "https://api.capenetworks.com"
API = "/networking-uxi/v1alpha1"


class UxiClient(BaseClient):
    product = "uxi"
    prefixes = (f"{API}/",)
    probe_path = f"{API}/sensors"

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None = DEFAULT_BASE_URL,
        client_id: str | None,
        client_secret: str | None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        token_url: str = GREENLAKE_TOKEN_URL,
    ) -> None:
        super().__init__(
            gate=gate,
            base_url=base_url or DEFAULT_BASE_URL,
            transport=transport,
            timeout=timeout,
            has_login=bool(client_id and client_secret),
        )
        login_http = Http("uxi-login", timeout=timeout, transport=transport)  # type: ignore[arg-type]
        self._creds = ClientCredentials("uxi", login_http, token_url, client_id or "", client_secret or "")

    @classmethod
    def from_env(cls, gate: Gate, logins: Mapping[str, str] | None = None, **kw: Any) -> UxiClient:
        """A client from the login variables (``logins``, or read from the environment)."""
        logins = read_logins() if logins is None else logins
        return cls(
            gate=gate,
            base_url=logins.get("UXI_BASE_URL") or DEFAULT_BASE_URL,
            client_id=logins.get("UXI_CLIENT_ID"),
            client_secret=logins.get("UXI_CLIENT_SECRET"),
            **kw,
        )

    def _login_headers(self) -> dict[str, str]:
        return {}

    async def _send(self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]) -> Any:
        return await send_bearer(self._http, self._creds, self.base_url, method, url, headers, params, body)

    async def aclose(self) -> None:
        await super().aclose()
        await self._creds.aclose()
