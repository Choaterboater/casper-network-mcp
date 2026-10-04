"""The Mist client: ``Authorization: Token <MIST_API_TOKEN>`` against ``MIST_HOST``.

Adapted from mist-mcp's ``mist_mcp/client.py`` (the ``request()`` gate shape,
``MistAPIError`` and ``_extract_detail``), made async on ``core/http.py``.
Dropped: the read-only env switch (the read-only pin is ``--read-only``),
the auth-scheme switch, and every env file: the login comes only from the
process environment Casper sets.
"""

from __future__ import annotations

from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused

__all__ = ["DEFAULT_HOST", "ApiError", "LoginMissing", "MistClient", "Refused"]

#: Mist Global 01, the first server in the bundled spec; ``MIST_HOST`` picks another cloud.
DEFAULT_HOST = "api.mist.com"


def _base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if "://" in host else f"https://{host}"


class MistClient(BaseClient):
    product = "mist"
    prefixes = ("/api/v1/",)
    probe_path = "/api/v1/self"

    def __init__(
        self,
        *,
        gate: Gate,
        token: str | None,
        base_url: str | None = None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = token or ""
        super().__init__(
            gate=gate,
            base_url=base_url or _base_url(DEFAULT_HOST),
            transport=transport,
            timeout=timeout,
            has_login=bool(token),
        )

    @classmethod
    def from_env(cls, gate: Gate, **kw: Any) -> MistClient:
        logins = read_logins()
        return cls(
            gate=gate,
            token=logins.get("MIST_API_TOKEN"),
            base_url=_base_url(logins.get("MIST_HOST", DEFAULT_HOST)),
            **kw,
        )

    def _login_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._token}"}
