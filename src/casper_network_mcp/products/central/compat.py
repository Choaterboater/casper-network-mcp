"""What the copied Central tools call: ``get_client()``, ``get_mcp_client()`` and their helpers.

The ~250 Central tools copied from hpe-networking-mcp (``monitoring``,
``config``, ``ops``, ``nac``; MIT, nowireless4u/hpe-networking-mcp) were
written against that project's ``CentralClient`` (``get``/``post``/``_request``
...) and ``shared.py`` helpers. This module keeps that shape so the tools
port with few edits, but every method sends through the gated
:class:`~casper_network_mcp.products.central.client.CentralClient`
(``request_sync``/``exchange_sync`` or their async twins): the read-only pin
first, then the path check, then the login. There is no other road to Central.

What changed from the source:

* ``_request(..., diagnostic=True)`` declares the call ``troubleshoot`` (the
  source's flag for a non-mutating troubleshooting POST); the gate lets it
  through ``--read-only`` only when the operation is on ``TROUBLESHOOT_OPS``;
* ``get``/``post``/``put``/``patch``/``delete`` raise :class:`CentralHTTPError`
  (an ``ApiError`` carrying ``.response`` like the source's httpx error) on
  an error status; ``_request`` returns a :class:`Reply` with its status;
* troubleshooting endpoints are ``/network-troubleshooting/v1/...`` only
  (the bundled documents' version; the source's env switch for v1alpha1 is gone);
* ``maybe_bound`` always bounds (its env switch is gone);
* replies are bounded and have secret values hidden by the client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from casper_network_mcp.core.budget import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, bound_collection_response, clamp_limit
from casper_network_mcp.core.http import compact_http_error
from casper_network_mcp.core.paths import path_segment, safe_api_path
from casper_network_mcp.products._base import ApiError, Reply
from casper_network_mcp.products._tools import client

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "IDENTITY_STORES_PATH",
    "MAC_ADDRESS_STORE_NAME",
    "MAX_LIST_LIMIT",
    "POLL_INTERVAL",
    "POLL_MAX",
    "CentralHTTPError",
    "CentralShim",
    "Reply",
    "WriteResultError",
    "atroubleshoot_async",
    "atroubleshoot_poll",
    "bound_collection_response",
    "clamp_limit",
    "compact_http_error",
    "device_type_for_troubleshoot",
    "find_identity_store_id",
    "get_client",
    "get_mcp_client",
    "maybe_bound",
    "resp_json",
    "safe_api_path",
    "seg",
    "troubleshooting_endpoint_candidates",
    "validate_write_result",
]

logger = logging.getLogger(__name__)


class CentralHTTPError(ApiError):
    """Central answered with an error status. ``.response`` is the :class:`Reply`."""

    def __init__(self, response: Reply, method: str = "") -> None:
        self.response = response
        super().__init__("central", response.status_code, response.data, url=response.url, method=method)


_SENT_KWARGS = ("params", "json", "headers")


def _split_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep the request parts the gated client sends; ``retry_5xx`` and the like are dropped."""
    unknown = set(kwargs) - {*_SENT_KWARGS, "retry_5xx", "max_retries", "timeout"}
    if unknown:
        raise TypeError(f"unsupported Central request arguments: {sorted(unknown)}")
    return {k: kwargs[k] for k in _SENT_KWARGS if kwargs.get(k) is not None}


def _parse_json(reply: Reply) -> dict[str, Any]:
    """The source's ``_parse_json``: ``{}`` for no body, ``{"items": [...]}`` for a list."""
    data = reply.data
    if data is None or data == "":
        return {}
    if isinstance(data, str):
        raise ValueError(f"Central sent a reply that is not JSON ({reply.url})")
    return data if isinstance(data, dict) else {"items": data}


class CentralShim:
    """The source ``CentralClient`` surface, sending only through the gated client."""

    def __init__(self, gated: Any) -> None:
        self._gated = gated

    # ── raw replies (status kept) ──────────────────────────────────────────

    def _request(
        self, method: str, endpoint: str, max_retries: int = 3, *, diagnostic: bool = False, **kwargs: Any
    ) -> Reply:
        kind = "troubleshoot" if diagnostic else None
        reply: Reply = self._gated.exchange_sync(method, endpoint, kind=kind, **_split_kwargs(kwargs))
        return reply

    async def _arequest(
        self, method: str, endpoint: str, max_retries: int = 3, *, diagnostic: bool = False, **kwargs: Any
    ) -> Reply:
        kind = "troubleshoot" if diagnostic else None
        reply: Reply = await self._gated.exchange(method, endpoint, kind=kind, **_split_kwargs(kwargs))
        return reply

    # ── checked calls (raise on an error status) ───────────────────────────

    def _checked(self, method: str, endpoint: str, **kwargs: Any) -> Reply:
        reply = self._request(method, endpoint, **kwargs)
        if not reply.is_success:
            raise CentralHTTPError(reply, method)
        return reply

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return _parse_json(self._checked("GET", endpoint, params=params))

    async def aget(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = await self._arequest("GET", endpoint, params=params)
        if not reply.is_success:
            raise CentralHTTPError(reply, "GET")
        return _parse_json(reply)

    def post(
        self, endpoint: str, data: dict[str, Any] | list[Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return _parse_json(self._checked("POST", endpoint, json=data, params=params))

    def post_async(
        self, endpoint: str, data: dict[str, Any] | list[Any] | None = None, params: dict[str, Any] | None = None
    ) -> str:
        """POST to an async endpoint; return the Location header (the task address)."""
        return str(self._checked("POST", endpoint, json=data, params=params).headers.get("Location", ""))

    def patch(
        self, endpoint: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return _parse_json(self._checked("PATCH", endpoint, json=data, params=params))

    def put(
        self, endpoint: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return _parse_json(self._checked("PUT", endpoint, json=data, params=params))

    def delete(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return _parse_json(self._checked("DELETE", endpoint, params=params))


def get_client() -> CentralShim:
    """The Central client the copied tools call (the gated client, source-shaped)."""
    return CentralShim(client("central"))


def get_mcp_client() -> Any:
    """The source's read adapter (device, site, client and alert lookups) over :func:`get_client`."""
    from casper_network_mcp.products.central.reads import MCPClient

    return MCPClient(get_client())


# ── ids in paths ────────────────────────────────────────────────────────────


def seg(value: Any) -> str:
    """One id in a Central path: refused if it holds ``/ \\ ? # %``, ``..`` or control characters.

    The copied tools put ids from the AI straight into their paths; every such
    id now goes through this, so an id can never change which endpoint is
    called (the request sent is the one Casper's box showed). Readable
    characters such as ``:`` in a MAC stay as they are.
    """
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    path_segment(text)  # raises UnsafePath (a ValueError) for anything unsafe
    return quote(text, safe="-._~:@")


# ── NAC identity stores ─────────────────────────────────────────────────────

#: The built-in Central NAC store for MAC authentication, found by name (its id differs per tenant).
MAC_ADDRESS_STORE_NAME = "MAC Address Store"
IDENTITY_STORES_PATH = "/network-config/v1alpha1/identity-stores"


def find_identity_store_id(central: CentralShim, name: str = MAC_ADDRESS_STORE_NAME) -> str:
    """The id of the NAC identity store called ``name`` (``GET .../identity-stores``, field ``store``).

    The source hard-coded one tenant's id; this reads it from the tenant
    being changed. Raises ``ValueError`` (a plain error to the AI) if no
    store has that name.
    """
    data = central.get(IDENTITY_STORES_PATH)
    stores = data.get("store") or data.get("items") or []
    wanted = name.strip().lower()
    for store in stores if isinstance(stores, list) else []:
        if isinstance(store, dict) and str(store.get("name", "")).strip().lower() == wanted and store.get("id"):
            return str(store["id"])
    raise ValueError(f"Central has no NAC identity store named {name!r}; list them with list_identity_stores.")


# ── shared helpers (from hpe-networking-mcp shared.py) ──────────────────────


class WriteResultError(RuntimeError):
    """A write's reply says the change was refused although no error was raised."""


def validate_write_result(result: Any, *, context: str = "") -> Any:
    """Fail closed on a non-2xx or error-shaped write result; return ``result`` unchanged."""
    where = f"{context}: " if context else ""
    status_code = getattr(result, "status_code", None)
    if status_code is not None:
        is_success = getattr(result, "is_success", None)
        if not isinstance(is_success, bool):
            try:
                is_success = 200 <= int(status_code) < 300
            except (TypeError, ValueError):
                is_success = False
        if not is_success:
            raise WriteResultError(f"{where}{compact_http_error(result)}")
        return result
    if isinstance(result, Mapping):
        errors = result.get("errors")
        if isinstance(errors, (list, tuple)) and any(item not in (None, "", [], {}) for item in errors):
            raise WriteResultError(f"{where}write reported errors: {list(errors)}")
        if isinstance(errors, str) and errors.strip():
            raise WriteResultError(f"{where}write reported error: {errors}")
        if isinstance(errors, Mapping) and len(errors) > 0:
            raise WriteResultError(f"{where}write reported errors: {dict(errors)}")
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            raise WriteResultError(f"{where}write reported error: {error}")
        for flag_name in ("success", "ok"):
            if result.get(flag_name) is False:
                raise WriteResultError(f"{where}write reported {flag_name}=False")
        status_field = result.get("status")
        if isinstance(status_field, str) and status_field.strip().lower() in {"failed", "failure", "error"}:
            raise WriteResultError(f"{where}write reported status={status_field!r}")
        result_status_code = result.get("status_code")
        if isinstance(result_status_code, int) and not (200 <= result_status_code < 300):
            raise WriteResultError(f"{where}write reported status_code={result_status_code}")
    return result


def maybe_bound(data: Any, *, limit: int, offset: int = 0, list_key: str | None = None) -> Any:
    """Bound a list reply (the source made this an env opt-in; here it always bounds)."""
    return bound_collection_response(data, limit=limit, offset=offset, list_key=list_key)


def _truncate_text(value: Any, max_chars: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def resp_json(resp: Any) -> dict[str, Any]:
    """``resp.json()``, or compact metadata when the body is not JSON."""
    try:
        body: dict[str, Any] = resp.json()
        return body if isinstance(body, dict) else {"items": body}
    except Exception:
        raw_text = resp.text or ""
        return {"status_code": resp.status_code, "text_preview": _truncate_text(raw_text), "text_length": len(raw_text)}


# ── troubleshooting ─────────────────────────────────────────────────────────

#: Seconds between polls of a troubleshooting task, and how many polls (about a minute).
POLL_INTERVAL: float = 5
POLL_MAX = 12
_TROUBLESHOOTING_VERSION = "v1"


def troubleshooting_endpoint_candidates(segment: str, serial_number: str, action: str) -> list[str]:
    """The troubleshooting endpoint for a device type, serial and action (v1, the bundled version)."""

    def path_piece(value: str, label: str) -> str:
        normalized = str(value).strip()
        if (
            not normalized
            or len(normalized) > 128
            or normalized in {".", ".."}
            or any(char in "/\\?#%" or char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
        ):
            raise ValueError(f"{label} contains invalid URL path characters")
        return quote(normalized, safe="-._~")

    parts = (path_piece(segment, "segment"), path_piece(serial_number, "serial_number"), path_piece(action, "action"))
    return [f"/network-troubleshooting/{_TROUBLESHOOTING_VERSION}/" + "/".join(parts)]


async def atroubleshoot_poll(central: CentralShim, poll_url: str) -> dict[str, Any]:
    """Poll a troubleshooting task without blocking the event loop."""
    result: dict[str, Any] = {}
    for _ in range(POLL_MAX):
        await asyncio.sleep(POLL_INTERVAL)
        try:
            result = await central.aget(poll_url)
        except Exception as exc:
            return {"status": "ERROR", "error": str(exc)}
        if result.get("status", "") in ("COMPLETED", "FAILED"):
            return result
    return result


def _async_response_location(resp: Any) -> str:
    header: str = resp.headers.get("Location", "")
    if header:
        return header
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    location = body.get("location") or body.get("Location")
    return location if isinstance(location, str) else ""


async def atroubleshoot_async(
    central: CentralShim,
    endpoint: str | list[str],
    payload: dict[str, Any],
    errors: list[str],
    *,
    diagnostic: bool = False,
) -> dict[str, Any]:
    """Start a troubleshooting task and poll it.

    ``diagnostic=True`` declares a hand-checked check (ping, show ...) that
    changes nothing; a disruptive action (PoE or port bounce, reboot) leaves
    it false, so the read-only pin refuses it.
    """
    candidates = [endpoint] if isinstance(endpoint, str) else list(endpoint)
    if not candidates:
        errors.append("no troubleshooting endpoint candidates provided")
        return {"status": None, "errors": errors}

    poll_url: str | None = None
    candidate = candidates[0]
    for index, candidate in enumerate(candidates):
        is_last = index == len(candidates) - 1
        resp = await central._arequest("POST", candidate, json=payload, diagnostic=diagnostic)
        if resp.status_code == 404 and not is_last:
            continue
        if resp.status_code not in (200, 201, 202):
            errors.append(compact_http_error(resp))
            return {"status": None, "errors": errors}
        location = _async_response_location(resp)
        if not location:
            errors.append("no Location header in async response")
            return {"status": None, "errors": errors}
        task_id = location.rstrip("/").split("/")[-1]
        poll_url = f"{candidate}/async-operations/{task_id}"
        break

    if poll_url is None:
        errors.append("no working troubleshooting endpoint among candidates")
        return {"status": None, "errors": errors}

    result = await atroubleshoot_poll(central, poll_url)
    result["errors"] = errors
    result["endpoint_used"] = candidate
    return result


_DTYPE_MAP = {
    "AP": "aps",
    "ACCESS_POINT": "aps",
    "CX": "cx",
    "AOS_CX": "cx",
    "AOS-CX": "cx",
    "AOSCX": "cx",
    "AOS_S": "aos-s",
    "AOS-S": "aos-s",
    "AOSS": "aos-s",
    "GATEWAY": "gateways",
    "GW": "gateways",
}

# Model series that tell AOS-CX from AOS-S when the firmware version is missing.
_CX_MODEL_SERIES = (
    "4100",
    "6000",
    "6100",
    "6200",
    "6300",
    "6400",
    "8100",
    "8320",
    "8325",
    "8360",
    "8400",
    "9300",
    "10000",
)
_AOS_S_MODEL_SERIES = ("2530", "2540", "2620", "2920", "2930", "3810", "5400")


def _classify_switch(device: dict[str, Any]) -> str:
    """Tell an AOS-CX switch ('cx') from an AOS-S one ('aos-s') from its inventory record."""
    fw = device.get("firmwareVersion") or device.get("softwareVersion") or device.get("swVersion") or ""
    fw_upper = str(fw).upper()
    if fw_upper:
        parts = fw_upper.split(".")
        prefix = parts[0]
        major = next((p for p in parts if p.isdigit()), "")
        if major == "10":
            return "cx"
        if major == "16":
            return "aos-s"
        if prefix and len(prefix) == 2 and prefix.isalpha():
            return "aos-s"
    model = str(device.get("model") or device.get("deviceModel") or "")
    if any(series in model for series in _CX_MODEL_SERIES):
        return "cx"
    if any(series in model for series in _AOS_S_MODEL_SERIES):
        return "aos-s"
    logger.warning("Ambiguous switch type (firmware=%r model=%r); defaulting to 'cx'", fw, model)
    return "cx"


def device_type_for_troubleshoot(serial_number: str, device_type: str | None) -> str | None:
    """The troubleshooting URL device type ("aps", "cx", "aos-s", "gateways"), from the argument or inventory."""
    if device_type:
        upper = device_type.upper()
        if upper in _DTYPE_MAP:
            return _DTYPE_MAP[upper]
        if upper not in ("SWITCH", "SWITCHES"):
            return upper.lower()
    device = get_mcp_client().get_device_by_serial(serial_number)
    if not device:
        return None
    raw = device.get("deviceType", "")
    if "ACCESS_POINT" in raw or raw == "AP":
        return "aps"
    if "SWITCH" in raw:
        return _classify_switch(device)
    if "GATEWAY" in raw:
        return "gateways"
    return None
