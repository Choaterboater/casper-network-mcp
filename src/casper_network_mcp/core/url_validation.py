"""Check a product base URL before any login is attached to it.

Adapted from hpe-networking-mcp's ``pipeline/url_validation.py``. Changes: no environment switches. Private
and on-site hosts are allowed (ClearPass usually lives on one; the person's
own login is the limit), plain ``http`` is allowed only for this machine, and
unedited documentation placeholder hosts are always refused.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

__all__ = ["is_loopback_host", "placeholder_host_reason", "validate_infra_url"]

_LOCAL_HOST_NAMES = {"localhost", "localhost.localdomain"}

# RFC 2606 / RFC 6761 documentation and testing names: a base URL that still
# points at one was never edited.
_RESERVED_PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net", "example.edu")
_RESERVED_PLACEHOLDER_TLDS = frozenset({"example", "invalid", "test"})
_PLACEHOLDER_HOST_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "replaceme",
    "replace-me",
    "replace_me",
    "placeholder",
    "yourhost",
    "your-host",
    "your_host",
    "yourdomain",
    "your-domain",
    "your_domain",
    "your-org",
    "your-company",
    "your-tenant",
    "fill-me-in",
    "fqdn-here",
    "hostname-here",
    "<",
    ">",
)


def placeholder_host_reason(host: str) -> str | None:
    """Say why ``host`` looks like an unedited placeholder, or return ``None``."""
    candidate = host.strip().lower().strip("[]").rstrip(".")
    if not candidate:
        return None
    for marker in _PLACEHOLDER_HOST_MARKERS:
        if marker in candidate:
            return f"contains the placeholder word {marker!r}"
    tld = candidate.rsplit(".", 1)[-1]
    if tld in _RESERVED_PLACEHOLDER_TLDS:
        return f"uses the reserved example ending '.{tld}'"
    for domain in _RESERVED_PLACEHOLDER_DOMAINS:
        if candidate == domain or candidate.endswith(f".{domain}"):
            return f"is under the reserved example domain {domain!r}"
    return None


def is_loopback_host(host: str) -> bool:
    """True if ``host`` is this machine only (127.0.0.0/8, ::1, localhost)."""
    candidate = host.strip().lower().strip("[]")
    if candidate in _LOCAL_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def validate_infra_url(value: str, *, label: str) -> str:
    """Return ``value`` without a trailing slash, or raise ``ValueError``.

    Needs an absolute ``https://host`` URL with no user name or password in
    it and a host that is not an example placeholder. ``http`` is accepted
    only for this machine.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an absolute HTTPS URL")  # noqa: TRY004 - one error type for callers
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not include a user name or password")
    if parsed.scheme not in {"https", "http"}:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    host = (parsed.hostname or "").strip().lower()
    if not parsed.netloc or not host:
        raise ValueError(f"{label} must include a host name")
    reason = placeholder_host_reason(host)
    if reason is not None:
        raise ValueError(
            f"{label} host {host!r} {reason}; it is still an example value, not a "
            "real server. Use the real host name; no login is sent to it."
        )
    if parsed.scheme != "https" and not is_loopback_host(host):
        raise ValueError(f"{label} must use https (plain http only works for this machine)")
    return base_url
