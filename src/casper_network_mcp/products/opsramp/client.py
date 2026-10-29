"""The OpsRamp client: an OpsRamp custom integration's key and secret on the tenant's API host.

The login is ``OPSRAMP_BASE_URL`` (``https://<tenant>.api.opsramp.com``; a trailing ``/api`` or
``/api/v2`` is dropped), ``OPSRAMP_TENANT_ID`` (the client or partner id), ``OPSRAMP_CLIENT_ID`` and
``OPSRAMP_CLIENT_SECRET``. It logs in at ``POST /tenancy/auth/oauth/token`` (client credentials,
form body); the token lives in memory only. Every path is under ``/api/v2/tenants/<tenant>/``.

Search queries go out with spaces as ``%20``: OpsRamp reads ``+`` as its field separator.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.http import Http
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.core.paths import path_segment
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused
from casper_network_mcp.products._oauth import ClientCredentials, send_bearer, with_query

__all__ = ["TENANTS", "TOKEN_PATH", "ApiError", "LoginMissing", "OpsRampClient", "Refused", "clean_base_url"]

TOKEN_PATH = "/tenancy/auth/oauth/token"
TENANTS = "/api/v2/tenants"


def clean_base_url(url: str | None) -> str | None:
    """``https://acme.api.opsramp.com/api/`` -> ``https://acme.api.opsramp.com``."""
    if not url:
        return url
    url = url.strip().rstrip("/")
    for suffix in ("/api/v2", "/api"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


class OpsRampClient(BaseClient):
    product = "opsramp"
    prefixes = (f"{TENANTS}/",)
    probe_path = ""

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None,
        tenant_id: str | None,
        client_id: str | None,
        client_secret: str | None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.tenant_id = (tenant_id or "").strip()
        super().__init__(
            gate=gate,
            base_url=clean_base_url(base_url),
            transport=transport,
            timeout=timeout,
            has_login=bool(self.tenant_id and client_id and client_secret),
        )
        login_http = Http("opsramp-login", timeout=timeout, transport=transport)  # type: ignore[arg-type]
        self._creds = ClientCredentials(
            "opsramp", login_http, f"{self.base_url}{TOKEN_PATH}", client_id or "", client_secret or ""
        )

    @classmethod
    def from_env(cls, gate: Gate, logins: Mapping[str, str] | None = None, **kw: Any) -> OpsRampClient:
        """A client from the login variables (``logins``, or read from the environment)."""
        logins = read_logins() if logins is None else logins
        return cls(
            gate=gate,
            base_url=logins.get("OPSRAMP_BASE_URL"),
            tenant_id=logins.get("OPSRAMP_TENANT_ID"),
            client_id=logins.get("OPSRAMP_CLIENT_ID"),
            client_secret=logins.get("OPSRAMP_CLIENT_SECRET"),
            **kw,
        )

    @property
    def tenant_path(self) -> str:
        """``/api/v2/tenants/<tenant>``; with no login, the call is refused as login_missing."""
        if not self.has_login:
            raise LoginMissing("opsramp")
        return f"{TENANTS}/{path_segment(self.tenant_id)}"

    def _login_headers(self) -> dict[str, str]:
        return {}

    async def _send(self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]) -> Any:
        return await send_bearer(
            self._http, self._creds, self.base_url, method, with_query(url, params), headers, None, body
        )

    async def aclose(self) -> None:
        await super().aclose()
        await self._creds.aclose()
