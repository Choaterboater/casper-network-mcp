"""Exact API lookups: ``METHOD /path`` or an operationId straight to its generated tool.

The idea comes from hpe-networking-mcp ``pipeline/clients/capability_coverage.py``
(MIT, nowireless4u/hpe-networking-mcp), rewritten over the router catalog:
a path may be the spec template (``/api/v1/sites/{site_id}``) or a concrete
path (``/api/v1/sites/s1``); a fixed segment wins over a ``{param}``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol, TypeVar

__all__ = ["exact_hit", "parse_method_path"]


class _Operation(Protocol):
    @property
    def origin(self) -> str: ...
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def operation_id(self) -> str: ...


Entry = TypeVar("Entry", bound=_Operation)

_METHOD_PATH = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)\s*$", re.IGNORECASE)
_OPERATION_ID = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_.-]{2,}\s*$")


def parse_method_path(query: str) -> tuple[str, str] | None:
    m = _METHOD_PATH.match(query)
    if not m:
        return None
    return m.group(1).upper(), m.group(2).split("?", 1)[0].rstrip("/") or "/"


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _match_path(method: str, path: str, entries: Iterable[Entry]) -> Entry | None:
    wanted = _segments(path)
    candidates = [e for e in entries if e.method == method and len(_segments(e.path)) == len(wanted)]
    for i, value in enumerate(wanted):
        if not candidates:
            return None
        fixed = [e for e in candidates if _segments(e.path)[i] == value]
        candidates = fixed or [e for e in candidates if _is_param(_segments(e.path)[i])]
    return candidates[0] if candidates else None


def exact_hit(query: str, entries: Iterable[Entry]) -> Entry | None:
    """The generated tool a ``METHOD /path`` or operationId query names, if any."""
    generated = [e for e in entries if e.origin == "generated"]
    parsed = parse_method_path(query)
    if parsed is not None:
        return _match_path(parsed[0], parsed[1], generated)
    if _OPERATION_ID.match(query) and " " not in query.strip():
        wanted = query.strip().lower()
        for entry in generated:
            if entry.operation_id and entry.operation_id.lower() == wanted:
                return entry
    return None
