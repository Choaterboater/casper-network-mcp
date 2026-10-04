"""The ClearPass client: ``Authorization: Bearer <CLEARPASS_API_TOKEN>`` against ``CLEARPASS_BASE_URL``.

ClearPass is usually on a private address on site; that is allowed (the
login is the limit). Its API lives under ``/api``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.logins import read_logins
from casper_network_mcp.products._base import ApiError, BaseClient, LoginMissing, Refused

__all__ = ["ApiError", "ClearPassClient", "LoginMissing", "Refused"]


class ClearPassClient(BaseClient):
    product = "clearpass"
    prefixes = ("/api/",)
    probe_path = "/api/oauth/me"

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None,
        token: str | None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = token or ""
        super().__init__(gate=gate, base_url=base_url, transport=transport, timeout=timeout, has_login=bool(token))

    @classmethod
    def from_env(cls, gate: Gate, logins: Mapping[str, str] | None = None, **kw: Any) -> ClearPassClient:
        """A client from the login variables (``logins``, or read from the environment)."""
        logins = read_logins() if logins is None else logins
        return cls(gate=gate, base_url=logins.get("CLEARPASS_BASE_URL"), token=logins.get("CLEARPASS_API_TOKEN"), **kw)

    def _login_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}
