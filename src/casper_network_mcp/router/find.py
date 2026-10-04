"""``find_tool``: exact API lookups first, then keyword matches over tool names.

Ported from hpe-networking-mcp ``mcp_servers/tool_router.py`` (``_query_tokens``,
``_keyword_hits``, ``_exact_discovery_hit``, ``find_tool``; MIT,
nowireless4u/hpe-networking-mcp). The semantic (embedding) pass is gone: this
server has no vector store. A hit needs at least one query word in the tool's
name; words in the first line of its description add to the score but never
count alone. Generated tools (one per API operation, thousands of them) rank
below the hand-written ones unless the query names their operation.
"""

from __future__ import annotations

from typing import Any

from casper_network_mcp.router.exact import exact_hit
from casper_network_mcp.router.index import Entry, catalog, tokens

__all__ = ["find_tool", "query_tokens"]

_STOPWORDS = frozenset(
    {
        "list",
        "get",
        "set",
        "find",
        "the",
        "a",
        "an",
        "of",
        "for",
        "to",
        "on",
        "at",
        "in",
        "and",
        "or",
        "all",
        "one",
        "new",
        "show",
        "view",
        "with",
        "from",
        "into",
        "via",
        "use",
        "using",
        "please",
        "make",
        "create",
        "build",
        "generate",
        "my",
        "is",
        "are",
        "what",
        "which",
        "how",
    }
)

#: Scope words that add a little weight when the tool also takes that argument.
_SCOPE_QUERY_TERMS = frozenset({"serial", "site", "org", "scope", "mac"})

#: Generated tools sit a full point below curated ones for plain-language queries.
_GENERATED_PENALTY = 1.0

#: Words that name a product, so "mist sites" prefers Mist tools.
_PRODUCT_WORDS = {
    "mist": "mist",
    "central": "central",
    "aruba": "central",
    "clearpass": "clearpass",
    "cppm": "clearpass",
}


def query_tokens(query: str) -> set[str]:
    """Distinctive words of a query (two letters or more, stopwords removed)."""
    return {t for t in tokens(query) if len(t) >= 2 and t not in _STOPWORDS}


def _score(entry: Entry, words: set[str], query_low: str, product_hint: str | None) -> float | None:
    name_words = entry.name_tokens - _STOPWORDS
    overlap = words & name_words
    if not overlap:
        return None
    precision = len(overlap) / max(len(name_words), 1)
    score = float(len(words & (name_words | entry.summary_tokens))) + precision
    if len(overlap) >= 2:
        score += 0.15
    if entry.origin == "generated":
        score -= _GENERATED_PENALTY
        if entry.operation_id and entry.operation_id.lower() in query_low:
            score += 4.0
        if entry.path and entry.path.lower() in query_low:
            score += 3.0
    params = {str(p).lower() for p in (entry.schema.get("properties") or {})}
    score += 0.35 * len(words & _SCOPE_QUERY_TERMS & {p.split("_")[0] for p in params})
    if product_hint and entry.product == product_hint:
        score += 0.5
    return score


def _hit(entry: Entry, include_schema: bool) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "name": entry.name,
        "product": entry.product,
        "summary": entry.summary,
        "kind": entry.kind,
        "label": entry.label,
    }
    if include_schema:
        hit["schema"] = entry.schema
    return hit


def find_tool(
    query: str,
    top_k: int = 5,
    product: str | None = None,
    include_schema: bool = False,
) -> list[dict[str, Any]]:
    """Up to ``top_k`` (1-10) inner tools for ``query``, best first."""
    top_k = max(1, min(int(top_k), 10))
    wanted_product = (product or "").strip().lower() or None
    entries = [e for e in catalog().entries.values() if wanted_product is None or e.product == wanted_product]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    exact = exact_hit(query, entries)
    if exact is not None:
        out.append(_hit(exact, include_schema))
        seen.add(exact.name)

    words = query_tokens(query)
    hint = next((_PRODUCT_WORDS[w] for w in words if w in _PRODUCT_WORDS), None)
    query_low = query.lower()
    scored: list[tuple[float, str, Entry]] = []
    for entry in entries:
        if entry.name in seen:
            continue
        score = _score(entry, words, query_low, hint)
        if score is not None:
            scored.append((score, entry.name, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _, _, entry in scored[: top_k - len(out)]:
        out.append(_hit(entry, include_schema))
    return out
