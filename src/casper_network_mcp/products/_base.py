"""What the three product clients share: one gated request path and one error shape.

``request()`` is the only way a tool reaches a product. In order it:

1. asks the gate (``core/gate.py``) -- the read-only pin, and from Task 12 the
   login's scopes -- and is refused before anything else happens;
2. checks the path (``core/paths.safe_api_path``): relative, inside the
   product's API, no dot segments, no encoded slashes, no query or fragment;
3. checks there is a login and a usable product address;
4. sends, with the login added last so no argument can replace it;
5. hides secret values in the reply (``core/redact.py``).

The error shape ``{status, detail, request_id, url}`` follows mist-mcp's
``MistAPIError`` and ``_extract_detail`` (mist-mcp ``mist_mcp/client.py``).
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from casper_network_mcp.core.budget import bounded_response_payload
from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.gate import PRODUCT_NAMES, Gate, LoginMissing, Refused
from casper_network_mcp.core.http import Http, body_kwargs
from casper_network_mcp.core.paths import safe_api_path, validate_product_base_url
from casper_network_mcp.core.redact import redact_sensitive, redact_tool_error_text
from casper_network_mcp.openapi_gen.runtime import is_auth_param, is_transport_header

__all__ = ["ApiError", "BaseClient", "LoginMissing", "Refused", "Reply"]

_REQUEST_ID_HEADERS = ("x-request-id", "x-mist-request-id", "x-correlation-id")


def _extract_detail(payload: bytes) -> Any:
    """The useful part of an error body (from mist-mcp's ``_extract_detail``)."""
    try:
        data = jsonlib.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", "replace")[:2000]
    if isinstance(data, dict):
        detail = data.get("detail")
        if detail is None:
            detail = {k: v for k, v in data.items() if k != "request_id"}
        return detail
    return data


class ApiError(ToolError):
    """The product answered with an error status."""

    def __init__(
        self,
        product: str,
        status: int,
        detail: Any = None,
        *,
        request_id: str | None = None,
        url: str | None = None,
        method: str = "",
    ) -> None:
        self.product = product
        self.status = status
        self.detail = redact_sensitive(detail)
        self.request_id = request_id
        self.url = url
        name = PRODUCT_NAMES.get(product, product)
        where = f" to {method} {url}" if url else ""
        text = self.detail if isinstance(self.detail, str) else jsonlib.dumps(self.detail, default=str)
        message = f"{name} answered {status}{where}"
        if self.detail not in (None, "", {}):
            message += f": {text[:500]}"
        super().__init__(redact_tool_error_text(message))

    def as_error(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "status": self.status,
            "detail": self.detail,
            "request_id": self.request_id,
            "url": self.url,
        }


_REPLY_HEADERS = ("location", "content-type", *_REQUEST_ID_HEADERS)


class Reply:
    """A product reply that keeps its status: what the copied Central code reads off a response.

    The body is already bounded and has secret values hidden; only a few
    harmless headers (Location, content type, request id) are kept.
    """

    def __init__(self, status_code: int, data: Any, headers: dict[str, str] | None = None, url: str = "") -> None:
        self.status_code = status_code
        self.data = data
        self.headers = httpx.Headers(headers or {})
        self.url = url

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        if self.data is None:
            return ""
        if isinstance(self.data, str):
            return self.data
        return jsonlib.dumps(self.data, default=str)

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def json(self) -> Any:
        if isinstance(self.data, str):
            return jsonlib.loads(self.data)  # raises ValueError for a non-JSON body
        if self.data is None:
            raise ValueError("the reply has no body")
        return self.data

    def raise_for_status(self) -> None:
        if not self.is_success:
            detail = _extract_detail(self.content) if self.data is not None else None
            raise ApiError("central", self.status_code, detail, url=self.url)


class BaseClient:
    """The shared request path. Subclasses set the product, prefixes and login headers."""

    product: str = ""
    prefixes: tuple[str, ...] = ()
    #: A plain read used in tests and by ``access_check``.
    probe_path: str = ""

    def __init__(
        self,
        *,
        gate: Gate,
        base_url: str | None,
        transport: httpx.MockTransport | httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        has_login: bool = True,
    ) -> None:
        self.gate = gate
        self.timeout = timeout
        self._transport = transport
        self._has_login = has_login and bool(base_url)
        self._address_error: str | None = None
        self.base_url = ""
        if base_url:
            try:
                self.base_url = validate_product_base_url(base_url, product=PRODUCT_NAMES[self.product]).rstrip("/")
            except ValueError as exc:
                self._address_error = str(exc)
        self._http = Http(self.product, timeout=timeout, transport=transport)  # type: ignore[arg-type]

    # ── the steps before sending ────────────────────────────────────────────

    def _prepare(
        self,
        method: str,
        path: str,
        *,
        kind: str | None,
        path_args: dict[str, Any] | None,
    ) -> str:
        """Gate, path check and login check; return the URL to send to."""
        method = method.upper()
        self.gate.check(self.product, method, path, dict(path_args or {}), kind)
        safe_api_path(path, self.prefixes)
        if not self._has_login:
            raise LoginMissing(self.product)
        if self._address_error is not None:
            name = PRODUCT_NAMES[self.product]
            raise Refused(f"Not sent: the {name} address is not usable ({self._address_error}).")
        return f"{self.base_url}{path}"

    def _headers(self, extra: dict[str, str] | None, body_headers: dict[str, str]) -> dict[str, str]:
        headers = {
            k: str(v)
            for k, v in (extra or {}).items()
            if v is not None and not is_auth_param(k) and not is_transport_header(k)
        }
        headers.update(body_headers)
        headers["Accept"] = "application/json"
        return headers

    def _login_headers(self) -> dict[str, str]:
        raise NotImplementedError

    # ── replies ─────────────────────────────────────────────────────────────

    def _decode(self, resp: httpx.Response, method: str, url: str) -> Any:
        if resp.status_code >= 400:
            request_id = next((resp.headers[h] for h in _REQUEST_ID_HEADERS if h in resp.headers), None)
            raise ApiError(
                self.product,
                resp.status_code,
                _extract_detail(resp.content),
                request_id=request_id,
                url=url,
                method=method,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return redact_sensitive(bounded_response_payload(resp))

    def _reply(self, resp: httpx.Response, url: str) -> Reply:
        """The reply with its status kept, for code that checks the status itself."""
        headers = {k: resp.headers[k] for k in _REPLY_HEADERS if k in resp.headers}
        if resp.status_code >= 400:
            try:
                data: Any = redact_sensitive(jsonlib.loads(resp.content))
            except (ValueError, UnicodeDecodeError):
                data = resp.content.decode("utf-8", "replace")[:2000]
        elif resp.status_code == 204 or not resp.content:
            data = None
        else:
            data = redact_sensitive(bounded_response_payload(resp))
        return Reply(resp.status_code, data, headers, url)

    # ── the async front door ────────────────────────────────────────────────

    async def _send(self, method: str, url: str, headers: dict[str, str], params: Any, body: dict[str, Any]) -> Any:
        return await self._http.request(
            method, url, headers={**headers, **self._login_headers()}, params=params, **body
        )

    async def request(
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
        """Send one request through the gate; return the reply with secrets hidden."""
        method = method.upper()
        url = self._prepare(method, path, kind=kind, path_args=path_args)
        body, body_headers = body_kwargs(json, content_type)
        resp = await self._send(method, url, self._headers(headers, body_headers), params, body)
        return self._decode(resp, method, url)

    async def exchange(
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
        """Like :meth:`request` (same gate and checks), but an error status comes back as a ``Reply``."""
        method = method.upper()
        url = self._prepare(method, path, kind=kind, path_args=path_args)
        body, body_headers = body_kwargs(json, content_type)
        resp = await self._send(method, url, self._headers(headers, body_headers), params, body)
        return self._reply(resp, url)

    async def aclose(self) -> None:
        await self._http.aclose()
