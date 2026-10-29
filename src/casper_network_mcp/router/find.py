"""``find_tool``: exact API lookups first, then words ranked over the prebuilt index.

Ported from hpe-networking-mcp ``mcp_servers/tool_router.py`` (``_query_tokens``,
``_keyword_hits``, ``_exact_discovery_hit``, ``find_tool``). The semantic (embedding) pass is gone: this
server has no vector store. Measured on a fixed 60-question set
(``scripts/bench_find_tool.py``), then tuned one change at a time:

* plurals fold to one word, and ``router/synonyms.yaml`` adds the words tool
  names use for the words people use ("access point" -> ap, "kick" ->
  disconnect, "who is on" -> client); each typed word with its synonyms is
  one idea and counts once;
* a hit needs a question word in the tool's name; words in the first line
  of its description add to the score but never count alone; the product
  prefix in a name does not dilute it; generated tools (one per API
  operation) rank a point below hand-written ones unless the query names
  their operation or path;
* the question's first word after conversational filler and product names gives
  its intent (what/show -> a read,
  create/delete/change -> that kind of change, reboot/upgrade -> an action,
  ping/traceroute -> a check), and tools that match it rank higher;
* answers come from ``router/index.json`` (see ``router.index``), so the
  first question does not load any backend.
"""

from __future__ import annotations

import functools
import re
from importlib.resources import files
from typing import Any

import yaml  # type: ignore[import-untyped]

from casper_network_mcp.router.exact import exact_hit
from casper_network_mcp.router.index import IndexEntry, catalog, fold, load_index

__all__ = ["find_tool", "intent", "query_groups", "query_tokens", "synonyms"]

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
    "uxi": "uxi",
    "opsramp": "opsramp",
}


#: Filler before the first real word of a question ("please", "can you", "I want to").
_FILLER = frozenset(
    {"please", "can", "could", "would", "you", "i", "we", "want", "need", "to", "me", "let", "us", "go"}
)

#: What the first real word of a question asks for.
_INTENT_WORDS = {
    "read": frozenset(
        {
            "what",
            "which",
            "who",
            "whom",
            "how",
            "is",
            "are",
            "was",
            "were",
            "why",
            "where",
            "when",
            "show",
            "list",
            "get",
            "find",
            "look",
            "see",
            "view",
            "display",
            "check",
            "does",
            "did",
            "do",
            "any",
            "give",
            "tell",
        }
    ),
    "create": frozenset({"create", "make", "add", "new", "build", "claim", "register", "onboard"}),
    "delete": frozenset({"delete", "remove", "release", "drop", "purge"}),
    "update": frozenset(
        {
            "change",
            "update",
            "set",
            "edit",
            "rename",
            "modify",
            "move",
            "assign",
            "disable",
            "enable",
            "turn",
            "tag",
            "extend",
            "block",
            "mark",
        }
    ),
    "act": frozenset({"reboot", "restart", "reload", "upgrade", "downgrade", "bounce", "shut", "re-enable"}),
    "check": frozenset({"ping", "traceroute", "trace"}),
}

#: Name words that say which of those a tool does.
_ACTION_WORDS = {
    "create": frozenset({"create", "add", "build", "claim", "upsert", "register"}),
    "delete": frozenset({"delete", "remove"}),
    "update": frozenset({"update", "set", "change", "replace", "patch", "assign", "unassign", "move", "enabled"}),
}

#: How much a tool that does what the question asks gains, and one that does not loses.
_INTENT_MATCH = 1.0
_INTENT_MISMATCH = 0.75


def intent(query: str) -> str | None:
    """Intent from the first word after filler/product names; None when unclear."""
    for word in re.findall(r"\bre-enable\b|[a-z0-9']+", query.lower()):
        if word in _FILLER or word in _PRODUCT_WORDS:
            continue
        return next((name for name, cues in _INTENT_WORDS.items() if word in cues), None)
    return None


def _action(entry: IndexEntry) -> str:
    if entry.kind == "read":
        return "read"
    if entry.kind == "troubleshoot":
        return "check"
    if entry.kind == "delete":
        return "delete"
    for name, cues in _ACTION_WORDS.items():
        if entry.name_tokens & cues:
            return name
    if entry.kind in ("disruptive", "firmware"):
        return "act"
    return "other"


def _intent_bonus(entry: IndexEntry, wanted: str | None) -> float:
    if wanted is None:
        return 0.0
    action = _action(entry)
    # A check (ping, show commands) answers a question as a read does.
    matched = action == wanted or (wanted == "read" and action == "check")
    return _INTENT_MATCH if matched else -_INTENT_MISMATCH


def _folded_text(text: str) -> str:
    """Use the same word boundaries and singular forms for synonym keys and questions."""
    return " ".join(fold(w) for w in re.split(r"[^a-z0-9]+", text.lower()) if w)


@functools.cache
def synonyms(product: str | None = None) -> tuple[tuple[re.Pattern[str], frozenset[str]], ...]:
    """Folded word or phrase patterns and the words they add, longest phrase first."""
    raw = yaml.safe_load((files("casper_network_mcp.router") / "synonyms.yaml").read_text(encoding="utf-8")) or {}
    # Router vocabulary additions and corrections, without changing the shipped index.
    raw.update(
        {
            "failed": ["failure"],
            "denied": ["failure"],
            "add": ["create"],
            "connection": ["event"],
            "interface": ["port"],
            "drop": ["error", "flapping"],
            "dropping": ["flapping"],
            "reconnect": ["flapping"],
            "reconnecting": ["flapping"],
            "flap": ["flapping"],
            "shut": ["bounce"],
            "re-enable": ["bounce"],
            "extend": ["update"],
            "block": ["update"],
            "mark": ["update"],
        }
    )
    if product == "clearpass":
        raw.update({"connected": ["client", "session"], "online": ["session"], "switch": ["network", "device"]})
    folded: dict[str, set[str]] = {}
    for key, values in raw.items():
        folded.setdefault(_folded_text(str(key)), set()).update(fold(str(w).lower()) for w in values)
    return tuple(
        (re.compile(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"), frozenset(folded[key]))
        for key in sorted(folded, key=lambda k: (-len(k), k))
    )


def query_groups(query: str, product: str | None = None) -> list[frozenset[str]]:
    """The question's ideas: each typed word with its synonyms, and each matched phrase's words.

    A word that a synonym spreads into several tool words ("wifi" -> wlan,
    ssid, wireless) is still one idea, so it counts once when tools are ranked.
    """
    low = _folded_text(query)
    typed = list(dict.fromkeys(low.split()))
    product = product or next((_PRODUCT_WORDS[w] for w in typed if w in _PRODUCT_WORDS), None)
    groups: dict[str, set[str]] = {t: {t} for t in typed if len(t) >= 2 and t not in _STOPWORDS}
    phrases: list[set[str]] = []
    for pattern, extra in synonyms(product):
        match = pattern.search(low)
        if not match:
            continue
        key = match.group(0)
        if key in groups:
            groups[key] |= extra
        else:
            phrases.append(set(extra))
    out: list[frozenset[str]] = []
    for group in [*groups.values(), *phrases]:
        frozen = frozenset(group)
        if frozen not in out:
            out.append(frozen)
    return out


def query_tokens(query: str) -> set[str]:
    """Distinctive words of a query (two letters or more, stopwords removed), plus their synonyms."""
    return set().union(*query_groups(query))


def _score(
    entry: IndexEntry,
    groups: list[frozenset[str]],
    query_low: str,
    product_hint: str | None,
    wanted: str | None = None,
) -> float | None:
    words = set().union(*groups) if groups else set()
    name_words = entry.name_tokens - _STOPWORDS
    overlap = words & name_words
    if not overlap:
        return None
    # The product prefix (mist_, clearpass_) is not part of what a tool does.
    own_words = name_words - _PRODUCT_WORDS.keys()
    precision = len(overlap & own_words) / max(len(own_words), 1)
    known = name_words | entry.summary_tokens
    score = float(sum(1 for group in groups if group & known)) + precision
    if sum(1 for group in groups if group & name_words) >= 2:
        score += 0.15
    if entry.origin == "generated":
        score -= _GENERATED_PENALTY
        if entry.operation_id and entry.operation_id.lower() in query_low:
            score += 4.0
        if entry.path and entry.path.lower() in query_low:
            score += 3.0
    score += 0.35 * len(words & _SCOPE_QUERY_TERMS & entry.params)
    if product_hint and entry.product == product_hint:
        score += 0.5
    return score + _intent_bonus(entry, wanted)


def _read_form_preference(entry: IndexEntry, query_words: set[str]) -> int:
    """Break list/get ties using explicit collection cues or a plural resource noun."""
    if entry.kind != "read":
        return 0
    plural_resources = {fold(w) for w in query_words if fold(w) != w and fold(w) not in _STOPWORDS}
    collection = bool(query_words & {"list", "all"} or plural_resources & entry.name_tokens)
    return int(("list" if collection else "get") in entry.name_tokens)


def _hit(entry: IndexEntry, include_schema: bool) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "name": entry.name,
        "product": entry.product,
        "summary": entry.summary,
        "kind": entry.kind,
        "label": entry.label,
    }
    if include_schema:
        # Schemas are not in the word index; asking for one loads the tools themselves.
        real = catalog().get(entry.name)
        hit["schema"] = real.schema if real is not None else {}
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
    index = load_index()

    def allowed(entry: IndexEntry) -> bool:
        return wanted_product is None or entry.product == wanted_product

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    exact = exact_hit(query, (e for e in index.entries if allowed(e)))
    if exact is not None:
        out.append(_hit(exact, include_schema))
        seen.add(exact.name)

    # Explicit filters and exact API matches take precedence over prose hints.
    # Several aliases for one product are unambiguous; several products are not.
    hints = {_PRODUCT_WORDS[w] for w in re.findall(r"[a-z0-9]+", query.lower()) if w in _PRODUCT_WORDS}
    hint = next(iter(hints)) if len(hints) == 1 else None
    if wanted_product is None and exact is None:
        wanted_product = hint
    groups = query_groups(query, wanted_product)
    words = set().union(*groups) if groups else set()
    query_low = query.lower()
    wanted = intent(query)
    # Only tools with a query word in their name can be hits (the index lists them per word).
    candidates = {i for word in words for i in index.name_postings.get(word, ())}
    query_words = {w for w in re.split(r"[^a-z0-9]+", query_low) if w}
    scored: list[tuple[float, int, str, IndexEntry]] = []
    for i in candidates:
        entry = index.entries[i]
        if entry.name in seen or not allowed(entry):
            continue
        score = _score(entry, groups, query_low, hint, wanted)
        if score is not None:
            scored.append((score, _read_form_preference(entry, query_words), entry.name, entry))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    for _, _, _, entry in scored[: top_k - len(out)]:
        out.append(_hit(entry, include_schema))
    return out
