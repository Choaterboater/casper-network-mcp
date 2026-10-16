"""What the three product clients share: one gated request path and one error shape.

``request()`` is the only way a tool reaches a product. In order it:

1. asks the gate (``core/gate.py``) -- the read-only pin -- and is refused
   before anything else happens;
2. checks the path (``core/paths.safe_api_path``): relative, inside the
   product's API, no dot segments, no encoded slashes, no query or fragment;
3. checks there is a login and a usable product address;
   then, for a write, asks the gate whether the target is inside the
   login's ``can_change`` scopes (async front door only);
4. sends, with the login added last so no argument can replace it;
5. hides secret values in the reply (``core/redact.py``).

The error shape ``{status, detail, request_id, url}`` follows mist-mcp's
``MistAPIError`` and ``_extract_detail`` (mist-mcp ``mist_mcp/client.py``).
"""

from __future__ import annotations

import json as jsonlib
import re
from typing import Any

import httpx

from casper_network_mcp.core.budget import WHOLE_REPLY_MAX_BYTES, bounded_response_payload
from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.gate import PRODUCT_NAMES, Gate, LoginMissing, Refused, is_write
from casper_network_mcp.core.http import Http, body_kwargs, parse_retry_after
from casper_network_mcp.core.paths import safe_api_path, validate_product_base_url
from casper_network_mcp.core.redact import redact_sensitive, redact_tool_error_text
from casper_network_mcp.openapi_gen.runtime import is_auth_param, is_transport_header

__all__ = ["ApiError", "BaseClient", "LoginMissing", "Refused", "Reply", "TransportFailure"]

_REQUEST_ID_HEADERS = ("x-request-id", "x-mist-request-id", "x-correlation-id")


def _summarize_non_json(text: str) -> str:
    """A short, useful line for a body that is not JSON (often an HTML error page)."""
    head = text.lstrip()[:200].lower()
    if head.startswith(("<!doctype", "<html")) or "<html" in head:
        title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        label = " ".join(title.group(1).split()) if title else "HTML error page"
        return f"{label} (HTML body, {len(text.encode('utf-8', 'replace'))} bytes)"
    return text[:2000]


def _extract_detail(payload: bytes) -> Any:
    """The useful part of an error body (from mist-mcp's ``_extract_detail``)."""
    try:
        data = jsonlib.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return _summarize_non_json(payload.decode("utf-8", "replace"))
    if isinstance(data, dict):
        detail = data.get("detail")
        if detail is None:
            detail = {k: v for k, v in data.items() if k != "request_id"}
        return detail
    return data


def _retry_guidance(change: bool) -> str:
    if change:
        return "The outcome is unknown; check the target's current state before retrying this change."
    return "Retry this read after checking connectivity and service availability."


class TransportFailure(ToolError):
    """A transport failure with recovery guidance, never an automatic write retry."""

    def __init__(self, product: str, cause: httpx.TransportError, *, url: str, change: bool) -> None:
        self.product = product
        self.code = "timeout" if isinstance(cause, httpx.TimeoutException) else "connection_error"
        self.detail = redact_tool_error_text(str(cause))
        self.url = url
        self.guidance = _retry_guidance(change)
        name = PRODUCT_NAMES.get(product, product)
        super().__init__(f"Could not reach {name}: {self.detail}")

    def as_error(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": self.code,
            "product": self.product,
            "status": None,
            "detail": self.detail,
            "request_id": None,
            "url": self.url,
            "guidance": self.guidance,
        }


class ApiError(ToolError):
    """The product answered with an error status.

    A 401 means the login expired or is no longer good: the tool result is
    exactly ``{"error": "login_expired", "product": ...}`` (the same shape as
    ``login_missing``) so Casper can ask for a new one.
    """

    def __init__(
        self,
        product: str,
        status: int,
        detail: Any = None,
        *,
        request_id: str | None = None,
        url: str | None = None,
        method: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        self.product = product
        self.status = status
        self.detail = redact_sensitive(detail)
        self.request_id = request_id
        self.url = url
        self.retry_after_seconds = retry_after_seconds
        change = is_write(method, httpx.URL(url or "").path) if method else True
        if status == 403:
            self.code = "permission_denied"
            self.guidance = "Check the login's role and access to this target before trying again."
        elif status == 429:
            self.code = "rate_limited"
            self.guidance = "Wait for the rate limit to clear. " + _retry_guidance(change)
        elif status >= 500:
            self.code = "upstream_error"
            self.guidance = _retry_guidance(change)
        else:
            self.code = "api_error"
            self.guidance = "Check the request and target against the API documentation before trying again."
        name = PRODUCT_NAMES.get(product, product)
        where = f" to {method} {url}" if url else ""
        text = self.detail if isinstance(self.detail, str) else jsonlib.dumps(self.detail, default=str)
        if self.login_expired:
            message = f"The {name} login has expired or is not valid ({name} answered 401)."
        else:
            message = f"{name} answered {status}{where}"
            if self.detail not in (None, "", {}):
                message += f": {text[:500]}"
        super().__init__(redact_tool_error_text(message))

    @property
    def login_expired(self) -> bool:
        return self.status == 401

    def as_error(self) -> dict[str, Any]:
        if self.login_expired:
            return {"error": "login_expired", "product": self.product}
        return {
            "error": str(self),
            "status": self.status,
            "detail": self.detail,
            "request_id": self.request_id,
            "url": self.url,
            "code": self.code,
            "guidance": self.guidance,
            **({"retry_after_seconds": self.retry_after_seconds} if self.retry_after_seconds is not None else {}),
        }


_REPLY_HEADERS = ("location", "content-type", "retry-after", *_REQUEST_ID_HEADERS)


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

    @property
    def has_login(self) -> bool:
        """True when a login (and an address) is set for this product."""
        return self._has_login

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
                retry_after_seconds=parse_retry_after(resp.headers.get("Retry-After", "")),
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

    async def _send_checked(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: Any,
        body: dict[str, Any],
        *,
        kind: str | None = None,
    ) -> Any:
        try:
            return await self._send(method, url, headers, params, body)
        except httpx.TransportError as exc:
            path = url.removeprefix(self.base_url)
            raise TransportFailure(self.product, exc, url=url, change=is_write(method, path, kind)) from None

    async def preflight(self, method: str, path: str, *, path_args: dict[str, Any] | None = None) -> None:
        """Run the checks :meth:`request` runs first (gate, path, login, scope) and send nothing.

        For a change that reads before it writes: a change that would be
        refused is refused before the read.
        """
        method = method.upper()
        self._prepare(method, path, kind=None, path_args=path_args)
        await self.gate.check_scope(self.product, self, method, path, dict(path_args or {}))

    async def read_unredacted(self, path: str, *, path_args: dict[str, Any] | None = None) -> Any:
        """GET ``path`` through the gate with secrets kept, for a change that sends the whole object back.

        Only for building a request body: never return the result (or show it in a
        preview). A reply too large to keep whole is refused.
        """
        url = self._prepare("GET", path, kind="read", path_args=path_args)
        resp = await self._send_checked("GET", url, self._headers(None, {}), None, {})
        if resp.status_code >= 400 or resp.status_code == 204 or not resp.content:
            return self._decode(resp, "GET", url)
        try:
            whole = resp.json()
        except ValueError:
            whole = None
        # Size, not a comparison with bounded_response_payload: that also hides secrets, so a reply that
        # holds one would never compare equal even when it is whole.
        if whole is None or len(jsonlib.dumps(whole, ensure_ascii=False, default=str).encode()) > WHOLE_REPLY_MAX_BYTES:
            raise ToolError(f"Not sent: GET {path} did not return a whole JSON object.")
        return whole

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
        await self.gate.check_scope(self.product, self, method, path, dict(path_args or {}), kind)
        body, body_headers = body_kwargs(json, content_type)
        resp = await self._send_checked(method, url, self._headers(headers, body_headers), params, body, kind=kind)
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
        await self.gate.check_scope(self.product, self, method, path, dict(path_args or {}), kind)
        body, body_headers = body_kwargs(json, content_type)
        resp = await self._send_checked(method, url, self._headers(headers, body_headers), params, body, kind=kind)
        return self._reply(resp, url)

    async def aclose(self) -> None:
        await self._http.aclose()
