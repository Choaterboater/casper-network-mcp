"""Server-side shaping for ``invoke_read_tool``: every page, chosen fields, counts by a field.

A broad question ("how many APs per firmware?", "which tunnels are under a day old?") used to
mean pulling every page through the 16 KiB reply budget and parsing it on the AI's side. Here
the router does that work and hands back only what was asked for:

* ``all_pages`` calls the tool again with the continuation it returned (a ``next`` /
  ``next_cursor`` / ``cursor`` token, the next ``offset``, or the next ``page``) until the list
  ends, ``MAX_PAGES`` pages or ``MAX_ITEMS`` items, and joins the pages into one list.
* ``fields`` keeps only those keys of each item (``a.b`` reaches into nested objects).
* ``group_by`` replaces the items with a count per value of one field.

Only read tools are shaped (``invoke_read_tool``), so a repeated call never repeats a change.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from casper_network_mcp.core.budget import MAX_LIST_LIMIT

__all__ = ["MAX_ITEMS", "MAX_PAGES", "collect_pages", "group_items", "project_items", "shape"]

MAX_PAGES = 50
MAX_ITEMS = 10_000
#: Most distinct values a ``group_by`` lists; the rest are summed under ``_other``.
MAX_GROUPS = 100

_TOKEN_PARAMS = ("next_cursor", "next", "cursor")
_TOKEN_KEYS = ("next_cursor", "nextCursor", "next", "cursor")
_UNFOLLOWED_PARAMS = ("search_after", "start")

Call = Callable[[dict[str, Any]], Awaitable[Any]]


def primary_list(result: Any) -> tuple[str | None, list[Any] | None]:
    """The main list in a result: the result itself, or its longest top-level list (as the budget picks)."""
    if isinstance(result, list):
        return None, result
    if not isinstance(result, dict):
        return None, None
    candidates = [(k, v) for k, v in result.items() if k != "_pagination" and isinstance(v, list)]
    if not candidates:
        return None, None
    key, value = max(candidates, key=lambda kv: (len(kv[1]), kv[0]))
    return key, value


def _token(result: dict[str, Any]) -> Any:
    sources = [result]
    if isinstance(result.get("_pagination"), dict):
        sources.insert(0, result["_pagination"])
    for source in sources:
        for key in _TOKEN_KEYS:
            value = source.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                return value
    return None


def _next_arguments(
    params: set[str], args: dict[str, Any], result: Any, got: int, page_size: int | None
) -> dict[str, Any] | None:
    """The arguments for the next page, or ``None`` when the list has ended."""
    if got == 0:
        return None
    if isinstance(result, list):
        result = {"items": result}
    if not isinstance(result, dict):
        return None
    token = _token(result)
    token_param = next((p for p in _TOKEN_PARAMS if p in params), None)
    if token_param is not None:
        if token is None or token == args.get(token_param):
            return None
        return {**args, token_param: token}
    raw_pagination = result.get("_pagination")
    pagination: dict[str, Any] = raw_pagination if isinstance(raw_pagination, dict) else {}
    # Only the API's own top-level total: a hand-written tool's ``_pagination.total`` counts its page.
    total = result.get("total")
    short_page = page_size is not None and got < page_size
    if "offset" in params:
        offset = int(args.get("offset") or 0) + got
        if isinstance(total, int) and not isinstance(total, bool):
            return {**args, "offset": offset} if offset < total else None
        if pagination.get("truncated") or (page_size is not None and not short_page):
            return {**args, "offset": offset}
        return None
    if "page" in params:
        if short_page or page_size is None:
            return None
        return {**args, "page": int(args.get("page") or 1) + 1}
    return None


async def collect_pages(call: Call, args: dict[str, Any], params: set[str]) -> Any:
    """Call the tool page after page and join the pages' main lists into one result."""
    if not params & {*_TOKEN_PARAMS, "offset", "page"}:
        unfollowed = sorted(params & set(_UNFOLLOWED_PARAMS))
        result = await call(args)
        reason = f"follows {', '.join(unfollowed)}, which all_pages can't page" if unfollowed else "has no paging"
        return _with_pages(result, fetched=1, complete=not unfollowed, stopped=f"this tool {reason}")

    page_size: int | None = None
    chose_size = "limit" in params and not isinstance(args.get("limit"), int)
    if "limit" in params:
        page_size = MAX_LIST_LIMIT if chose_size else int(args["limit"])
        args = {**args, "limit": page_size}
    first = await call(args)
    if chose_size and isinstance(first, dict) and "error" in first:
        # The API may cap its page below ours: ask again at the tool's own default and learn the size.
        args = {k: v for k, v in args.items() if k != "limit"}
        first = await call(args)
        page_size = None
    if isinstance(first, dict) and "error" in first:
        return first
    key, items = primary_list(first)
    if items is None:
        return _with_pages(first, fetched=1, complete=True, stopped="no list in the reply")
    if page_size is None and "limit" in params and items:
        page_size = len(items)
    joined = list(items)
    fetched, stopped, resume = 1, "end of list", None
    current, result, previous = args, first, list(items)
    if _cut_locally(first) and params & set(_TOKEN_PARAMS):
        return _with_pages(
            first,
            fetched=1,
            complete=False,
            stopped="the first page came back cut short, so following its next token would skip items",
        )
    while True:
        following = _next_arguments(params, current, result, len(primary_list(result)[1] or []), page_size)
        if following is None:
            break
        if fetched >= MAX_PAGES or len(joined) >= MAX_ITEMS:
            stopped = f"stopped at {fetched} pages / {len(joined)} items"
            resume = {k: v for k, v in following.items() if k in (*_TOKEN_PARAMS, "offset", "page")}
            break
        result = await call(following)
        if isinstance(result, dict) and "error" in result:
            stopped = f"page {fetched + 1} failed: {result['error']}"
            resume = {k: v for k, v in following.items() if k in (*_TOKEN_PARAMS, "offset", "page")}
            break
        page = primary_list(result)[1] or []
        if _cut_locally(result) and params & set(_TOKEN_PARAMS):
            stopped = "a page came back cut short, so following its next token would skip items"
            resume = {k: v for k, v in following.items() if k in (*_TOKEN_PARAMS, "offset", "page")}
            joined.extend(page)
            fetched += 1
            break
        if page and page == previous:
            stopped = "the tool returned the same page again, so its paging was not followed further"
            break
        joined.extend(page)
        previous = page
        fetched += 1
        current = following
    merged: Any
    if key is None:
        merged = {"items": joined}
    else:
        merged = {k: v for k, v in first.items() if k not in ("_pagination", *_TOKEN_KEYS)}
        merged[key] = joined
    return _with_pages(merged, fetched=fetched, complete=resume is None, stopped=stopped, resume=resume)


def _cut_locally(result: Any) -> bool:
    """True when the tool cut the API's page down itself (its ``_pagination`` counts more than it kept)."""
    pagination = result.get("_pagination") if isinstance(result, dict) else None
    if not isinstance(pagination, dict) or not pagination.get("truncated"):
        return False
    _, items = primary_list(result)
    total = pagination.get("total")
    return isinstance(total, int) and items is not None and total > len(items)


def _with_pages(
    result: Any, *, fetched: int, complete: bool, stopped: str, resume: dict[str, Any] | None = None
) -> Any:
    if isinstance(result, list):
        result = {"items": result}
    if not isinstance(result, dict) or "error" in result:
        return result
    _, items = primary_list(result)
    pages: dict[str, Any] = {"fetched": fetched, "items": len(items or []), "complete": complete, "stopped": stopped}
    if resume:
        pages["resume_arguments"] = resume
    return {**result, "_pages": pages}


def _get(item: Any, path: str) -> Any:
    value = item
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def project_items(items: list[Any], fields: list[str]) -> list[Any]:
    """Each item cut down to ``fields`` (dotted paths become flat keys); non-objects are kept."""
    return [{f: _get(item, f) for f in fields} if isinstance(item, dict) else item for item in items]


def group_items(items: list[Any], path: str) -> dict[str, Any]:
    """How many items have each value of ``path``, most common first."""
    counts = Counter(_hashable(_get(item, path)) for item in items)
    ordered = counts.most_common()
    shown = dict(ordered[:MAX_GROUPS])
    if len(ordered) > MAX_GROUPS:
        shown["_other"] = sum(n for _, n in ordered[MAX_GROUPS:])
    return {"group_by": path, "total": len(items), "distinct": len(counts), "counts": shown}


def _hashable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return "null" if value is None else value
    return str(value)


def shape(result: Any, *, fields: list[str] | None, group_by: str | None) -> Any:
    """Apply ``fields`` then ``group_by`` to the result's main list; anything else is kept."""
    if (not fields and not group_by) or (isinstance(result, dict) and "error" in result):
        return result
    key, items = primary_list(result)
    if items is None:
        return result
    base = {"items": items} if key is None else dict(result)
    list_key = key or "items"
    if group_by:
        grouped = group_items(items, group_by)
        base.pop(list_key, None)
        base.pop("_pagination", None)
        return {**base, **grouped}
    base[list_key] = project_items(items, fields or [])
    return base
