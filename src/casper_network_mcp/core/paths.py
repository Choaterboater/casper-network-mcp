"""Safe URL paths: an id from the AI can never change which endpoint is called.

``safe_api_path`` and ``validate_product_base_url`` are adapted from
hpe-networking-mcp's ``shared.py`` and ``path_segment`` from its Mist
``_path_segment`` (MIT, nowireless4u/hpe-networking-mcp). ``path_segment`` is
stricter than the source: it refuses rather than quotes anything that could
move the request (``/``, ``?``, ``#``, ``.``/``..``, control characters, any
``%``), so the request sent is always the one Casper's box showed.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

from casper_network_mcp.core.url_validation import validate_infra_url

__all__ = ["UnsafePath", "path_segment", "safe_api_path", "validate_product_base_url"]


class UnsafePath(ValueError):
    """A path or path piece that could reach a different endpoint than intended."""


_ENCODED_PATH_RESERVED = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_ENCODED_PATH_DELIMITERS = re.compile(r"%(?:23|3f)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def path_segment(value: str) -> str:
    """Return ``value`` quoted for use as one URL path piece, or raise ``UnsafePath``."""
    if not isinstance(value, str):
        raise UnsafePath("an id in the path must be text")
    if value == "":
        raise UnsafePath("an id in the path must not be empty")
    if value in (".", ".."):
        raise UnsafePath("an id in the path must not be '.' or '..'")
    for char, name in (("/", "a slash"), ("\\", "a backslash"), ("?", "'?'"), ("#", "'#'"), ("%", "'%'")):
        if char in value:
            raise UnsafePath(f"an id in the path must not contain {name}")
    if _CONTROL.search(value):
        raise UnsafePath("an id in the path must not contain control characters")
    return quote(value, safe="")


def safe_api_path(path: str, prefix: str | tuple[str, ...]) -> str:
    """Check a relative API path and return it decoded, or raise ``UnsafePath``.

    The path must start with ``prefix`` (or one of several), carry no scheme,
    host, query or fragment, and have no dot segments, backslashes or encoded
    slashes, dots, ``?`` or ``#``.
    """
    allowed_prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
    if not isinstance(path, str):
        raise UnsafePath("path must be text")
    if _CONTROL.search(path):
        raise UnsafePath("path must not contain control characters")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or path.startswith("//"):
        raise UnsafePath("path must be a relative API path without scheme, host, query, or fragment")
    if _ENCODED_PATH_DELIMITERS.search(parsed.path):
        raise UnsafePath("path must not contain encoded query or fragment delimiters")
    if _ENCODED_PATH_RESERVED.search(parsed.path):
        raise UnsafePath("path must not contain encoded dot, slash, or backslash characters")
    decoded = unquote(parsed.path)
    if _ENCODED_PATH_DELIMITERS.search(decoded) or "?" in decoded or "#" in decoded:
        raise UnsafePath("path must not contain encoded query or fragment delimiters")
    if _ENCODED_PATH_RESERVED.search(decoded):
        raise UnsafePath("path must not contain double-encoded dot, slash, or backslash characters")
    if "\\" in decoded:
        raise UnsafePath("path must not contain backslashes")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise UnsafePath("path must not contain dot segments")
    if not decoded.startswith(allowed_prefixes):
        allowed = ", ".join(f"{p}*" for p in allowed_prefixes)
        raise UnsafePath(f"path must begin with one of: {allowed}")
    return decoded


def validate_product_base_url(url: str, *, product: str = "product") -> str:
    """Check a product base URL before a login is attached to it."""
    return validate_infra_url(url, label=f"{product} base URL")
