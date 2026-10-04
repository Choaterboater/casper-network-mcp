"""Hide secrets before a tool result or error reaches the AI.

Adapted from hpe-networking-mcp (MIT, nowireless4u/hpe-networking-mcp). Changes:
no environment reads, the marker is the plain word ``[hidden]``, and an
``auth`` object keeps its non-secret fields (its type) while its secrets are
hidden.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

__all__ = [
    "HIDDEN",
    "parse_stringified_container",
    "redact_sensitive",
    "redact_tool_error_text",
    "serialize_stringified_container",
]

HIDDEN = "[hidden]"

_SENSITIVE_KEY_EXACT = {
    "auth",
    "authorization",
    "key",
    "keys",
    "community_string",
    "snmp_read",
    "snmp_write",
}
_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "_key",
    "passphrase",
    "password",
    "psk",
    "secret",
    "token",
)
#: Keys whose *object* value is a settings block that mixes secrets with plain
#: settings (Mist ``wlan.auth`` holds ``type`` next to ``psk``). For these an
#: object is walked, so its type stays readable; a plain value is hidden.
_WALK_WHEN_OBJECT = {"auth"}


def _normalize_key(key: Any) -> str:
    """Fold a dict key or header name to a comparable ``snake_case`` token."""
    key_text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_KEY_EXACT or any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


# Some APIs return a field whose value is itself a JSON- or Python-repr-encoded
# dict/list serialized as one string. A secret nested in that blob would be
# invisible to a walker that only looks at the immediate key, so such strings
# are parsed, walked and re-encoded in the same style.
_CONTAINER_BRACKETS = {"{": "}", "[": "]", "(": ")"}
_MAX_STRINGIFIED_CONTAINER_LENGTH = 50_000
_CONTAINER_PARSE_ERRORS = (
    ValueError,
    TypeError,
    SyntaxError,
    RecursionError,
    MemoryError,
    OverflowError,
)


def parse_stringified_container(text: str) -> tuple[Any, str] | None:
    """Parse ``text`` as a JSON or Python-repr dict/list/tuple.

    Returns ``(parsed, dialect)`` with dialect ``"json"`` or ``"python"``, or
    ``None`` when ``text`` is not such a container. Never raises.
    """
    stripped = text.strip()
    if len(stripped) < 2 or len(stripped) > _MAX_STRINGIFIED_CONTAINER_LENGTH:
        return None
    if _CONTAINER_BRACKETS.get(stripped[0]) != stripped[-1]:
        return None
    try:
        return json.loads(stripped), "json"
    except _CONTAINER_PARSE_ERRORS:
        pass
    try:
        parsed = ast.literal_eval(stripped)
    except _CONTAINER_PARSE_ERRORS:
        return None
    if isinstance(parsed, (dict, list, tuple)):
        return parsed, "python"
    return None


def serialize_stringified_container(parsed: Any, dialect: str) -> str:
    """Re-encode a parsed container in its original dialect."""
    if dialect == "json":
        try:
            return json.dumps(parsed, default=str)
        except _CONTAINER_PARSE_ERRORS:
            return repr(parsed)
    return repr(parsed)


def redact_sensitive(value: Any) -> Any:
    """Return a copy of ``value`` with likely secrets replaced by ``[hidden]``."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            # bound_collection_response's "_pagination.list_key" trips the
            # "_key" rule; keep only that one field as is.
            if key == "_pagination" and isinstance(item, dict):
                scrubbed = redact_sensitive(dict(item))
                if "list_key" in item:
                    scrubbed["list_key"] = item["list_key"]
                out[key] = scrubbed
            elif _normalize_key(key) in _WALK_WHEN_OBJECT and isinstance(item, dict):
                out[key] = redact_sensitive(item)
            elif _is_sensitive_key(key):
                out[key] = HIDDEN
            else:
                out[key] = redact_sensitive(item)
        return out
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped.startswith(("bearer ", "token ", "basic ")):
            return HIDDEN
        blob = parse_stringified_container(value)
        if blob is not None:
            parsed, dialect = blob
            redacted_parsed = redact_sensitive(parsed)
            if redacted_parsed != parsed:
                return serialize_stringified_container(redacted_parsed, dialect)
    return value


# An error string can carry a bearer credential in the middle of the text,
# which the prefix rule above does not see. The {8,} minimum keeps short prose
# such as "Bearer token missing or expired" readable.
ERROR_CREDENTIAL_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=\-]{8,}")


def redact_tool_error_text(text: str) -> str:
    """Hide credentials in an error string a person or the AI may read."""
    text = redact_sensitive(text)
    return ERROR_CREDENTIAL_RE.sub(HIDDEN, text)
