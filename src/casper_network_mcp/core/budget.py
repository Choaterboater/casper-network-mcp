"""Bounded replies: list slicing, the router's response budget and resume cursors.

Adapted from hpe-networking-mcp's ``shared.py`` (``clamp_limit``,
``bound_collection_response``, ``bounded_response_payload``) and
``tool_router.py`` (response budget, HMAC continuation cursors). Changes: no environment overrides; the
budget and cursor lifetime are fixed constants a caller may pass explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

__all__ = [
    "CANONICAL_PAGINATION_KEYS",
    "CURSOR_MAX_LENGTH",
    "CURSOR_TTL_SECONDS",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "RESPONSE_BUDGET_BYTES",
    "RESPONSE_BUDGET_ITEMS",
    "CursorError",
    "bound_collection_response",
    "bound_router_response",
    "bounded_response_payload",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
]

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def clamp_limit(limit: int | None, default: int = DEFAULT_LIST_LIMIT) -> int:
    """Clamp a list limit to 1..MAX_LIST_LIMIT."""
    if limit is None:
        return default
    return max(1, min(limit, MAX_LIST_LIMIT))


#: ``_pagination`` members this module computes. Anything else in an incoming
#: ``_pagination`` block belongs to the backend (an opaque ``next_cursor``) and
#: is kept as is.
CANONICAL_PAGINATION_KEYS = frozenset({"offset", "limit", "total", "truncated", "list_key"})


def bound_collection_response(
    data: Any,
    *,
    limit: int,
    offset: int = 0,
    list_key: str | None = None,
) -> Any:
    """Slice the main list in a JSON value and add ``_pagination``.

    A list becomes ``{"items": [...], "_pagination": ...}``; a dict has
    ``list_key`` (or its longest top-level list) sliced. Anything else is
    returned unchanged.
    """
    lim = clamp_limit(limit)
    off = max(0, offset)
    if isinstance(data, list):
        total = len(data)
        page = data[off : off + lim]
        return {
            "items": page,
            "_pagination": {
                "offset": off,
                "limit": lim,
                "total": total,
                "truncated": total > off + len(page),
            },
        }
    if not isinstance(data, dict):
        return data
    existing_pagination = data.get("_pagination")
    out = {k: v for k, v in data.items() if k != "_pagination"}
    key = list_key
    if key is None:
        candidates = [(k, len(v)) for k, v in out.items() if isinstance(v, list)]
        if not candidates:
            return data
        key = max(candidates, key=lambda kv: (kv[1], kv[0]))[0]
    val = out.get(key)
    if not isinstance(val, list):
        return data
    total = len(val)
    if (
        off == 0
        and isinstance(existing_pagination, dict)
        and existing_pagination.get("offset", 0) == 0
        and existing_pagination.get("list_key", key) == key
        and isinstance(existing_pagination.get("total"), int)
    ):
        total = max(total, int(existing_pagination["total"]))
    page = val[off : off + lim]
    was_truncated = isinstance(existing_pagination, dict) and bool(existing_pagination.get("truncated"))
    out[key] = page
    pagination: dict[str, Any] = {
        "offset": off,
        "limit": lim,
        "total": total,
        "truncated": total > off + len(page) or was_truncated,
        "list_key": key,
    }
    if isinstance(existing_pagination, dict):
        carried = {k: v for k, v in existing_pagination.items() if k not in CANONICAL_PAGINATION_KEYS}
        if carried:
            pagination = {**carried, **pagination}
    out["_pagination"] = pagination
    return out


def bounded_response_payload(resp: Any, *, max_bytes: int = 131_072) -> Any:
    """Return JSON, bounded text, or bounded base64 details for an HTTP response."""
    raw = getattr(resp, "content", None)
    if raw is None:
        raw = str(getattr(resp, "text", "")).encode("utf-8", errors="replace")
    elif not isinstance(raw, bytes):
        raw = bytes(raw)

    headers = getattr(resp, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()
    size = len(raw)
    try:
        payload = resp.json()
    except (TypeError, ValueError):
        payload = None
    else:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode()
        if len(encoded) <= max_bytes:
            return payload
        collection_page = bound_collection_response(payload, limit=DEFAULT_LIST_LIMIT, offset=0)
        if collection_page is not payload:
            page_encoded = json.dumps(collection_page, ensure_ascii=False, default=str).encode()
            if len(page_encoded) <= max_bytes and isinstance(collection_page, dict):
                collection_page["_response_bounds"] = {
                    "content_type": "application/json",
                    "size_bytes": len(encoded),
                    "truncated": True,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
                return collection_page
        raw = encoded
        size = len(raw)
        content_type = "application/json"

    preview = raw[:max_bytes]
    truncated = size > len(preview)
    if content_type == "application/json" or content_type.endswith("+json"):
        return {
            "content_type": content_type,
            "size_bytes": size,
            "truncated": truncated,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": preview.decode("utf-8", errors="replace"),
        }
    if content_type.startswith("text/") or content_type in {
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }:
        return {
            "content_type": content_type or "text/plain",
            "size_bytes": size,
            "truncated": truncated,
            "text": preview.decode("utf-8", errors="replace"),
        }
    return {
        "content_type": content_type or "application/octet-stream",
        "size_bytes": size,
        "truncated": truncated,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "base64": base64.b64encode(preview).decode("ascii"),
    }


# ── Router response budget ──────────────────────────────────────────────────
#
# A fixed safety net applied to every routed tool result. A result within the
# budget is returned unchanged (same object, no new keys).

RESPONSE_BUDGET_ITEMS = MAX_LIST_LIMIT
#: Byte ceiling for one routed result. It matches the consumer's view (16 KiB): a
#: result this size or larger is sliced here and gets a ``next_cursor``, instead of
#: passing through whole and being cut blind downstream with no way to resume.
RESPONSE_BUDGET_BYTES = 16_384
_RESPONSE_BUDGET_MIN_ITEMS = 1
_RESPONSE_BUDGET_SHRINK_STEPS = 6


def _json_byte_size(value: Any) -> int | None:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _dict_primary_list_len(data: dict[str, Any]) -> tuple[str | None, int]:
    """Same list choice as bound_collection_response, so the pre-check agrees."""
    candidates = [(key, len(value)) for key, value in data.items() if key != "_pagination" and isinstance(value, list)]
    if not candidates:
        return None, 0
    key, length = max(candidates, key=lambda kv: (kv[1], kv[0]))
    return key, length


def _response_bounds_marker(
    *, reason: str, item_limit: int | None, byte_limit: int, size_bytes: int | None = None
) -> dict[str, Any]:
    marker: dict[str, Any] = {"truncated": True, "reason": reason, "byte_limit": byte_limit}
    if item_limit is not None:
        marker["item_limit"] = item_limit
    if size_bytes is not None:
        marker["size_bytes"] = size_bytes
    return marker


# ── Continuation cursors (read tools only) ──────────────────────────────────
#
# An opaque, signed token that lets the router resume a cut-short read. It
# carries only {version, expiry, next offset, tool-name digest, arguments
# digest}, never raw arguments, results or logins. The signing key is random
# per process, so a restart turns every old cursor into a plain error.

_CURSOR_VERSION = 1
CURSOR_TTL_SECONDS = 900
CURSOR_MAX_LENGTH = 512
CURSOR_DIGEST_HEX_CHARS = 16
_CURSOR_MAC_BYTES = 16
_CURSOR_HMAC_KEY = secrets.token_bytes(32)


class CursorError(Exception):
    """A cursor that is malformed, changed, expired or for other arguments.

    The message never includes raw arguments or secrets.
    """


def _cursor_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:CURSOR_DIGEST_HEX_CHARS]


def _cursor_args_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return _cursor_digest(canonical)


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign_cursor_payload(payload_bytes: bytes) -> bytes:
    return hmac.new(_CURSOR_HMAC_KEY, payload_bytes, hashlib.sha256).digest()[:_CURSOR_MAC_BYTES]


def encode_cursor(
    *, name: str, arguments: dict[str, Any], next_offset: int, ttl_seconds: int = CURSOR_TTL_SECONDS
) -> str:
    """Return a signed cursor that resumes ``name(arguments)`` at ``next_offset``."""
    payload = {
        "v": _CURSOR_VERSION,
        "exp": int(time.time()) + ttl_seconds,
        "off": int(next_offset),
        "t": _cursor_digest(name),
        "a": _cursor_args_digest(arguments),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _sign_cursor_payload(payload_bytes)
    return f"{_b64u_encode(payload_bytes)}.{_b64u_encode(signature)}"


def decode_cursor(cursor: str, *, name: str, arguments: dict[str, Any]) -> int:
    """Check ``cursor`` against ``name``/``arguments`` and return its offset.

    Raises ``CursorError`` for anything malformed, changed, expired, or made
    for another tool or other arguments (including a server restart).
    """
    if not isinstance(cursor, str) or not cursor:
        raise CursorError("cursor is missing or malformed")
    if len(cursor) > CURSOR_MAX_LENGTH:
        raise CursorError("cursor is longer than allowed")
    parts = cursor.split(".")
    if len(parts) != 2:
        raise CursorError("cursor is malformed")
    payload_b64, signature_b64 = parts
    try:
        payload_bytes = _b64u_decode(payload_b64)
        signature_bytes = _b64u_decode(signature_b64)
    except Exception as exc:
        raise CursorError("cursor is malformed") from exc
    if not hmac.compare_digest(signature_bytes, _sign_cursor_payload(payload_bytes)):
        raise CursorError("cursor is invalid (changed, or the server restarted)")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise CursorError("cursor is malformed") from exc
    if not isinstance(payload, dict):
        raise CursorError("cursor is malformed")
    if payload.get("v") != _CURSOR_VERSION:
        raise CursorError("cursor version is not supported")
    expiry = payload.get("exp")
    if not isinstance(expiry, int) or isinstance(expiry, bool):
        raise CursorError("cursor is malformed")
    if int(time.time()) >= expiry:
        raise CursorError("cursor has expired")
    offset = payload.get("off")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError("cursor is malformed")
    if payload.get("t") != _cursor_digest(name):
        raise CursorError("cursor is for a different tool")
    if payload.get("a") != _cursor_args_digest(arguments):
        raise CursorError("cursor is for different arguments")
    return offset


#: Keys a backend may use for its own continuation token, at the top level or
#: inside ``_pagination``. When one is present the router adds no cursor.
_UPSTREAM_CURSOR_KEYS = ("next_cursor", "nextCursor", "next", "cursor")


def _upstream_cursor_key(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in _UPSTREAM_CURSOR_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return key
    pagination = result.get("_pagination")
    if isinstance(pagination, dict):
        for key in _UPSTREAM_CURSOR_KEYS:
            value = pagination.get(key)
            if isinstance(value, str) and value.strip():
                return f"_pagination.{key}"
    return None


def bound_router_response(
    result: Any,
    *,
    max_items: int | None = None,
    max_bytes: int | None = None,
    offset: int = 0,
    enable_cursor: bool = False,
    tool_name: str | None = None,
    tool_arguments: dict[str, Any] | None = None,
    cursor_ttl_seconds: int = CURSOR_TTL_SECONDS,
) -> Any:
    """Bound one routed tool result to an item and byte budget.

    Adds ``_response_bounds`` only when something was cut, plus a
    ``next_cursor`` when ``enable_cursor`` is set (read tools only). Error
    dicts and scalars are never touched. A single item too big for the byte
    budget becomes a non-resumable text preview.
    """
    if isinstance(result, dict) and "error" in result:
        return result
    if not isinstance(result, (dict, list)):
        return result

    requested_items = max_items if max_items is not None else RESPONSE_BUDGET_ITEMS
    items_budget = max(1, min(requested_items, MAX_LIST_LIMIT))
    bytes_budget = max_bytes if max_bytes is not None else RESPONSE_BUDGET_BYTES
    resume_offset = max(0, int(offset or 0))

    if isinstance(result, list):
        primary_key, item_count = None, len(result)
        nothing_sliceable = False
    else:
        primary_key, item_count = _dict_primary_list_len(result)
        nothing_sliceable = primary_key is None

    if nothing_sliceable:
        resume_offset = 0

    size = _json_byte_size(result)
    remaining_count = max(0, item_count - resume_offset)
    item_overflow = remaining_count > items_budget
    byte_overflow = resume_offset == 0 and size is not None and size > bytes_budget
    if not (item_overflow or byte_overflow or resume_offset > 0):
        return result

    if nothing_sliceable:
        raw = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        marker = _response_bounds_marker(
            reason="byte_budget", item_limit=None, byte_limit=bytes_budget, size_bytes=len(raw)
        )
        marker["resumable"] = False
        marker["resumable_reason"] = "no_sliceable_collection"
        return {"_response_bounds": marker, "preview": raw[:bytes_budget].decode("utf-8", errors="replace")}

    limit = items_budget
    page = bound_collection_response(result, limit=limit, offset=resume_offset)
    encoded_size = _json_byte_size(page)
    byte_shrunk = False
    for _ in range(_RESPONSE_BUDGET_SHRINK_STEPS):
        if encoded_size is not None and encoded_size <= bytes_budget:
            break
        if limit <= _RESPONSE_BUDGET_MIN_ITEMS:
            break
        limit = max(_RESPONSE_BUDGET_MIN_ITEMS, limit // 2)
        byte_shrunk = True
        page = bound_collection_response(result, limit=limit, offset=resume_offset)
        encoded_size = _json_byte_size(page)

    if encoded_size is not None and encoded_size > bytes_budget:
        raw = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        marker = _response_bounds_marker(
            reason="byte_budget", item_limit=limit, byte_limit=bytes_budget, size_bytes=len(raw)
        )
        marker["resumable"] = False
        marker["resumable_reason"] = "single_item_exceeds_byte_budget"
        return {"_response_bounds": marker, "preview": raw[:bytes_budget].decode("utf-8", errors="replace")}

    slice_key = "items" if isinstance(result, list) else primary_key
    actual_count = len(page.get(slice_key, [])) if isinstance(page, dict) and slice_key else 0
    next_offset = resume_offset + actual_count
    pagination = page.get("_pagination") if isinstance(page, dict) else None
    truncated_by_items = bool(pagination and pagination.get("truncated"))
    if not (truncated_by_items or byte_shrunk):
        return page

    reasons = [r for r, present in (("item_budget", item_overflow), ("byte_budget", byte_shrunk)) if present] or [
        "item_budget"
    ]
    upstream_cursor = _upstream_cursor_key(page)
    can_emit_cursor = (
        enable_cursor
        and (truncated_by_items or byte_shrunk)
        and tool_name is not None
        and tool_arguments is not None
        and upstream_cursor is None
    )
    marker = _response_bounds_marker(reason="+".join(reasons), item_limit=limit, byte_limit=bytes_budget)
    marker["resumable"] = bool(can_emit_cursor)
    if upstream_cursor is not None:
        marker["resumable_reason"] = "upstream_cursor_present"
        marker["upstream_cursor_key"] = upstream_cursor
    if isinstance(page, dict):
        page = {**page, "_response_bounds": marker}
        if can_emit_cursor:
            assert tool_name is not None and tool_arguments is not None
            page["next_cursor"] = encode_cursor(
                name=tool_name, arguments=tool_arguments, next_offset=next_offset, ttl_seconds=cursor_ttl_seconds
            )
            page["cursor_expires_in_seconds"] = cursor_ttl_seconds
    return page
