"""Helpers the Mist tool modules share: the client, ids in paths, MACs and previews."""

from __future__ import annotations

import re
from typing import Any

from casper_network_mcp.core.paths import path_segment
from casper_network_mcp.products._tools import ToolSet, client, would_send

__all__ = [
    "MIST",
    "bool_param",
    "change",
    "extract_items",
    "get",
    "mist_client",
    "normalize_mac",
    "pick",
    "seg",
    "window",
]

#: Every hand-written Mist tool, from every module; ``tools.backend()`` registers them.
MIST = ToolSet("mist")


def mist_client() -> Any:
    return client("mist")


def seg(value: str) -> str:
    """One id in a path: refused if it could change which endpoint is called."""
    return path_segment(value)


def normalize_mac(mac_address: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", mac_address or "").lower()
    if len(normalized) != 12:
        raise ValueError("MAC address must contain exactly 12 hex characters")
    return normalized


def bool_param(value: bool | None) -> str | None:
    if value is None:
        return None
    return "true" if value else "false"


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    out = {k: v for k, v in params.items() if v is not None}
    return out or None


async def get(path: str, params: dict[str, Any] | None = None, **path_args: Any) -> Any:
    """A read through the gated client; empty params are left out."""
    return await mist_client().request("GET", path, params=_clean(params), kind="read", path_args=path_args)


async def change(
    method: str,
    path: str,
    *,
    body: Any = None,
    params: dict[str, Any] | None = None,
    dry_run: bool = False,
    preview_body: Any = None,
    **path_args: Any,
) -> Any:
    """Send one change, or with ``dry_run`` return what would be sent and send nothing."""
    if dry_run:
        return would_send(method, path, body if preview_body is None else preview_body, params)
    return await mist_client().request(method, path, params=_clean(params), json=body, path_args=path_args)


def window(duration: str | None, start: int | str | None, end: int | str | None) -> dict[str, Any]:
    """Mist's time window: start/end when given, else a duration ("1d" by default)."""
    if start or end:
        return {key: value for key, value in (("start", start), ("end", end)) if value}
    return {"duration": duration or "1d"}


def extract_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "results", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def pick(data: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return data
    return {field: data[field] for field in fields if field in data and data[field] not in (None, "")}
