"""The HTTP client every product client sends through.

Adapted from hpe-networking-mcp (MIT, nowireless4u/hpe-networking-mcp):
``pipeline/clients/pooled_clients.py`` (one pooled ``httpx.AsyncClient`` per
name and event loop), ``pipeline/clients/http_retry.py`` (bounded retries for
plain reads only) and ``shared.py`` (``compact_http_error``,
``response_payload``), and the request-body helpers from
``openapi_gen/http_exec.py``. No environment reads.

Writes are never retried: a change the server accepted and then throttled
must not be sent twice.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import itertools
import json as _json
import logging
import math
import random
import time
from typing import Any

import httpx

__all__ = [
    "RETRYABLE_STATUSES",
    "Http",
    "aclose_pooled_clients",
    "body_kwargs",
    "build_multipart_files",
    "compact_http_error",
    "get_with_retry",
    "parse_retry_after",
    "pooled_client",
    "request_read_retried",
    "response_payload",
]

logger = logging.getLogger(__name__)

# ── Pooled clients ──────────────────────────────────────────────────────────

_POOL: dict[str, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def pooled_client(
    name: str,
    *,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Return the shared client for ``name`` on the running loop, creating it once.

    ``timeout`` and ``transport`` apply only when the client is created.
    ``transport`` lets tests use ``httpx.MockTransport``.
    """
    loop = asyncio.get_running_loop()
    entry = _POOL.get(name)
    if entry is not None:
        client_loop, client = entry
        if client_loop is loop and not getattr(client, "is_closed", False):
            return client
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        transport=transport,
    )
    _POOL[name] = (loop, client)
    return client


async def aclose_pooled_clients() -> None:
    """Close every pooled client on the running loop and drop the rest."""
    loop = asyncio.get_running_loop()
    for name, (client_loop, client) in list(_POOL.items()):
        del _POOL[name]
        if client_loop is loop and not getattr(client, "is_closed", False):
            await client.aclose()


# ── Retries (plain reads only) ──────────────────────────────────────────────


def parse_retry_after(value: str) -> float | None:
    """Parse a Retry-After value (seconds or an HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    return max(0.0, target.timestamp() - time.time())


#: Short-lived upstream statuses worth a bounded retry on a plain read.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

_RETRY_INITIAL_DELAY = 0.5
_RETRY_MAX_DELAY = 8.0
_RETRY_AFTER_MAX_DELAY = 60.0


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = 2,
    **kwargs: Any,
) -> httpx.Response:
    """Send one GET, retrying 429/502/503/504 and dropped connections with backoff.

    GET only by construction: there is no method parameter.
    """
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(url, **kwargs)
        except httpx.TransportError:
            if attempt == max_retries:
                raise
            wait = min(delay, _RETRY_MAX_DELAY) * random.uniform(0.8, 1.2)
            logger.warning("Connection error on GET; retrying in %.1fs (%d/%d)", wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
            delay = min(delay * 2, _RETRY_MAX_DELAY)
            continue
        if response.status_code not in RETRYABLE_STATUSES or attempt == max_retries:
            return response
        hint = parse_retry_after(response.headers.get("Retry-After", ""))
        if hint is not None:
            wait = min(hint, _RETRY_AFTER_MAX_DELAY)
        else:
            wait = min(delay, _RETRY_MAX_DELAY) * random.uniform(0.8, 1.2)
        logger.warning(
            "HTTP %d on GET; retrying in %.1fs (%d/%d)", response.status_code, wait, attempt + 1, max_retries
        )
        await asyncio.sleep(wait)
        delay = min(delay * 2, _RETRY_MAX_DELAY)
    raise AssertionError("unreachable: the last attempt returns or raises above")


_BODY_KWARGS = frozenset({"json", "content", "data", "files"})


async def request_read_retried(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Retry only a GET without a body; send everything else exactly once."""
    if method.upper() == "GET" and not (_BODY_KWARGS & {k for k, v in kwargs.items() if v is not None}):
        clean = {k: v for k, v in kwargs.items() if k not in _BODY_KWARGS}
        return await get_with_retry(client, url, **clean)
    return await client.request(method, url, **kwargs)


# ── The client product clients use ──────────────────────────────────────────

_instance_ids = itertools.count(1)


class Http:
    """A pooled async HTTP client with read-only retries.

    Each instance gets its own pooled ``httpx.AsyncClient`` per event loop,
    closed by :func:`aclose_pooled_clients` when the server stops.
    """

    def __init__(
        self,
        name: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        self.name = name
        self._pool_key = f"{name}#{next(_instance_ids)}"
        self._timeout = timeout
        self._transport = transport
        self._max_retries = max_retries

    def client(self) -> httpx.AsyncClient:
        return pooled_client(self._pool_key, timeout=self._timeout, transport=self._transport)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: Any = None,
        json: Any = None,
    ) -> httpx.Response:
        method = method.upper()
        client = self.client()
        if method == "GET" and json is None:
            return await get_with_retry(client, url, max_retries=self._max_retries, headers=headers, params=params)
        return await client.request(method, url, headers=headers, params=params, json=json)

    async def aclose(self) -> None:
        entry = _POOL.pop(self._pool_key, None)
        if entry is not None and not entry[1].is_closed:
            await entry[1].aclose()


# ── Error text ──────────────────────────────────────────────────────────────


def response_payload(resp: Any) -> Any:
    """Return the JSON body, or the text for a body that is not JSON."""
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _truncate_text(value: Any, max_chars: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [cut {len(text) - max_chars} characters]"


def compact_http_error(resp: Any, endpoint: str | None = None, max_chars: int = 240) -> str:
    """Return a short ``HTTP <status> at <endpoint>: <body preview>`` message."""
    body = response_payload(resp)
    where = f" at {endpoint}" if endpoint else ""
    return f"HTTP {resp.status_code}{where}: {_truncate_text(body, max_chars=max_chars)}"


# ── Request bodies by content type ──────────────────────────────────────────

JSON_LIKE_CONTENT_TYPES = frozenset(
    {"application/json", "application/merge-patch+json", "application/scim+json", "application/json-patch+json"}
)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def build_multipart_files(
    body: Any, *, max_upload_bytes: int = MAX_UPLOAD_BYTES
) -> tuple[dict[str, tuple[Any, ...]] | None, str | None]:
    """Turn an MCP-safe multipart body into httpx file tuples, or return an error message.

    A file field is ``{"filename": ..., "content_base64": ..., "content_type": ...}``;
    objects and lists are sent as JSON parts and anything else as text.
    """
    if not isinstance(body, dict):
        return None, "A multipart body must be an object of form fields."
    files: dict[str, tuple[Any, ...]] = {}
    for key, value in body.items():
        field = str(key)
        if isinstance(value, bytes):
            files[field] = (field, value, "application/octet-stream")
        elif isinstance(value, dict) and "content_base64" in value:
            filename = str(value.get("filename") or field)
            if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
                return None, f"The file name for field {field!r} is not allowed."
            try:
                content = base64.b64decode(str(value["content_base64"]), validate=True)
            except (binascii.Error, ValueError):
                return None, f"Field {field!r} is not valid base64."
            if len(content) > max_upload_bytes:
                return None, f"Field {field!r} is larger than the {max_upload_bytes}-byte upload limit."
            files[field] = (filename, content, str(value.get("content_type") or "application/octet-stream"))
        elif isinstance(value, (dict, list)):
            files[field] = (None, _json.dumps(value), "application/json")
        else:
            files[field] = (None, "" if value is None else str(value))
    return files, None


def body_kwargs(body: Any, content_type: str) -> tuple[dict[str, Any], dict[str, str]]:
    """httpx keyword arguments and extra headers that send ``body`` as ``content_type``.

    Raises ``ValueError`` with a plain message when the body does not fit the type.
    """
    if body is None:
        return {}, {}
    content_type = (content_type or "application/json").split(";", 1)[0].strip().lower()
    if content_type in JSON_LIKE_CONTENT_TYPES:
        headers = {} if content_type == "application/json" else {"Content-Type": content_type}
        return {"json": body}, headers
    if content_type == "multipart/form-data":
        files, error = build_multipart_files(body)
        if error is not None:
            raise ValueError(error)
        return {"files": files}, {}
    if content_type == "application/x-www-form-urlencoded":
        if not isinstance(body, dict):
            raise ValueError("A form body must be an object of form fields.")
        return {"data": body}, {"Content-Type": content_type}
    return {"content": body if isinstance(body, (bytes, str)) else str(body)}, {"Content-Type": content_type}
