"""Exact API lookup over the bundled OpenAPI documents (SQLite + FTS5).

Adapted from hpe-networking-mcp ``pipeline/clients/specs_index.py``. Kept: build, connect, search, lookup,
get_endpoint, get_exact_endpoint, get_endpoint_by_operation_id, get_schema,
get_enum, get_response_description and the natural-language ranking. Changed:
the index is built from this package's own ``specs/`` on first use into the
user cache folder (``~/.cache/casper-network-mcp/specs-<hash>.sqlite``), with
a marker written last; a missing file, a missing marker (a run killed mid
build) or an unreadable file is simply rebuilt. Rows carry the product
(``central``/``mist``/``clearpass``) instead of source families and versions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from casper_network_mcp import specs_bundle

__all__ = [
    "build",
    "cache_dir",
    "clear_lookup_cache",
    "connect",
    "get_endpoint",
    "get_endpoint_by_operation_id",
    "get_enum",
    "get_exact_endpoint",
    "get_response_description",
    "get_schema",
    "index_path",
    "lookup",
    "marker_path",
    "search",
]

_INDEX_FORMAT = "1"

_SCHEMA = """
CREATE TABLE endpoints (
    id INTEGER PRIMARY KEY, product TEXT, source_url TEXT, identity TEXT,
    spec_name TEXT, spec_file TEXT, server TEXT,
    method TEXT, path TEXT, operation_id TEXT, summary TEXT, description TEXT
);
CREATE TABLE schemas (
    id INTEGER PRIMARY KEY, product TEXT, source_url TEXT, identity TEXT,
    spec_name TEXT, spec_file TEXT, name TEXT, description TEXT
);
CREATE TABLE fields (
    id INTEGER PRIMARY KEY, product TEXT, source_url TEXT, schema_identity TEXT,
    spec_name TEXT, spec_file TEXT, schema_name TEXT,
    field_name TEXT, path TEXT, type TEXT, description TEXT,
    enums TEXT, enum_descriptions TEXT
);
CREATE TABLE responses (
    id INTEGER PRIMARY KEY, product TEXT, spec_file TEXT, method TEXT, path TEXT,
    status_code TEXT, description TEXT
);
CREATE VIRTUAL TABLE fts USING fts5(
    kind, spec_file, ref, body, product UNINDEXED, source_url UNINDEXED, identity UNINDEXED
);
CREATE INDEX idx_fields_name ON fields(field_name);
CREATE INDEX idx_fields_identity ON fields(schema_identity);
CREATE INDEX idx_endpoints_path ON endpoints(path);
CREATE INDEX idx_endpoints_operation_id ON endpoints(operation_id COLLATE NOCASE);
CREATE INDEX idx_schemas_identity ON schemas(identity);
CREATE INDEX idx_responses_product_code ON responses(product, status_code);
"""

# ── Where the index lives ───────────────────────────────────────────────────


def cache_dir() -> Path:
    """The user cache folder for this package."""
    return Path.home() / ".cache" / "casper-network-mcp"


def _bundle_digest() -> str:
    data = (specs_bundle.specs_dir() / "MANIFEST.json").read_bytes()
    return hashlib.sha256(data + _INDEX_FORMAT.encode()).hexdigest()[:16]


def index_path() -> Path:
    """The cache file for the current bundle (its name changes with the bundle)."""
    return cache_dir() / f"specs-{_bundle_digest()}.sqlite"


def marker_path() -> Path:
    """Written after a build finished; without it the index is rebuilt."""
    path = index_path()
    return path.with_name(path.name + ".ok")


# ── Build ───────────────────────────────────────────────────────────────────


def _walk_fields(node: Any, path: str, depth: int = 0) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (field_path, field_name, definition), through items/allOf/anyOf/oneOf."""
    if depth > 12 or not isinstance(node, dict):
        return
    for field, fdef in (node.get("properties") or {}).items():
        if not isinstance(fdef, dict):
            continue
        fpath = f"{path}.{field}" if path else field
        yield fpath, field, fdef
        yield from _walk_fields(fdef, fpath, depth + 1)
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_fields(items, f"{path}[]", depth + 1)
    for comb in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(comb) or []:
            yield from _walk_fields(sub, path, depth + 1)


def _response_description(spec: dict[str, Any], resp: dict[str, Any]) -> str:
    """A response's description, following a ``#/components/responses/`` ref."""
    ref = resp.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/responses/"):
        name = ref.rsplit("/", 1)[-1]
        resolved = spec.get("components", {}).get("responses", {}).get(name)
        if isinstance(resolved, dict):
            return str(resolved.get("description", "") or "")
        return ""
    return str(resp.get("description", "") or "")


def _bundle_sources() -> list[tuple[str, str, str, dict[str, Any]]]:
    out = []
    for doc in specs_bundle.documents():
        out.append(
            (
                specs_bundle.product_for(doc),
                doc["path"],
                doc.get("source_url", ""),
                specs_bundle.load_document(doc["path"]),
            )
        )
    return out


def _dir_sources(specs_dir: Path) -> list[tuple[str, str, str, dict[str, Any]]]:
    out = []
    for path in sorted(specs_dir.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(spec, dict):
            product = specs_bundle.product_for({"path": path.name, "source_url": ""})
            if path.name.startswith("mist"):
                product = "mist"
            out.append((product, path.name, "", spec))
    return out


def _populate(conn: sqlite3.Connection, sources: list[tuple[str, str, str, dict[str, Any]]]) -> dict[str, int]:
    conn.executescript(_SCHEMA)
    counts = {"specs": 0, "endpoints": 0, "schemas": 0, "fields": 0, "responses": 0, "skipped": 0}
    for product, spec_file, source_url, spec in sources:
        info = spec.get("info")
        if not isinstance(info, dict):
            info = {}
        spec_name = str(info.get("title") or spec_file)
        servers = spec.get("servers") or []
        first = servers[0] if servers else {}
        server = first.get("url", "") if isinstance(first, dict) else ""
        records = 0
        for api_path, item in (spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                    continue
                summary = str(op.get("summary", "") or "")
                desc = str(op.get("description", "") or "")
                identity = f"{product}:{method.upper()} {api_path}"
                conn.execute(
                    "INSERT INTO endpoints (product, source_url, identity, spec_name, spec_file, server, method, "
                    "path, operation_id, summary, description) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        product,
                        source_url,
                        identity,
                        spec_name,
                        spec_file,
                        server,
                        method.upper(),
                        api_path,
                        str(op.get("operationId", "") or ""),
                        summary,
                        desc,
                    ),
                )
                conn.execute(
                    "INSERT INTO fts (kind, spec_file, ref, body, product, source_url, identity) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        "endpoint",
                        spec_file,
                        f"{method.upper()} {api_path}",
                        f"{spec_name} {api_path} {summary} {desc}",
                        product,
                        source_url,
                        identity,
                    ),
                )
                counts["endpoints"] += 1
                records += 1
                for status_code, resp in (op.get("responses") or {}).items():
                    if not isinstance(resp, dict) or not str(status_code).isdigit():
                        continue
                    resp_desc = _response_description(spec, resp)
                    if resp_desc:
                        conn.execute(
                            "INSERT INTO responses (product, spec_file, method, path, status_code, description) "
                            "VALUES (?,?,?,?,?,?)",
                            (product, spec_file, method.upper(), api_path, str(status_code), resp_desc),
                        )
                        counts["responses"] += 1
        components = spec.get("components")
        if not isinstance(components, dict):
            components = {}
        for schema_name, schema in (components.get("schemas") or {}).items():
            if not isinstance(schema, dict):
                continue
            s_desc = str(schema.get("description", "") or "")
            identity = f"{product}:{spec_file}:{schema_name}"
            prop_texts = []
            for fpath, field, fdef in _walk_fields(schema, ""):
                enums = fdef.get("enum")
                enum_desc = fdef.get("x-enumDescriptions")
                enum_json = json.dumps(enums) if enums else None
                enum_desc_json = json.dumps(enum_desc) if enum_desc else None
                field_desc = str(fdef.get("description", "") or "")
                conn.execute(
                    "INSERT INTO fields (product, source_url, schema_identity, spec_name, spec_file, schema_name, "
                    "field_name, path, type, description, enums, enum_descriptions) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        product,
                        source_url,
                        identity,
                        spec_name,
                        spec_file,
                        schema_name,
                        field,
                        fpath,
                        str(fdef.get("type", "")),
                        field_desc,
                        enum_json,
                        enum_desc_json,
                    ),
                )
                counts["fields"] += 1
                if enums or field_desc:
                    prop_texts.append(f"{field} {field_desc} {' '.join(map(str, enums or []))}")
            conn.execute(
                "INSERT INTO schemas (product, source_url, identity, spec_name, spec_file, name, description) "
                "VALUES (?,?,?,?,?,?,?)",
                (product, source_url, identity, spec_name, spec_file, schema_name, s_desc),
            )
            conn.execute(
                "INSERT INTO fts (kind, spec_file, ref, body, product, source_url, identity) VALUES (?,?,?,?,?,?,?)",
                (
                    "schema",
                    spec_file,
                    schema_name,
                    f"{spec_name} {schema_name} {s_desc} {' '.join(prop_texts)}",
                    product,
                    source_url,
                    identity,
                ),
            )
            counts["schemas"] += 1
            records += 1
        if records:
            counts["specs"] += 1
        else:
            counts["skipped"] += 1
    return counts


def build(db_path: Path, *, specs_dir: Path | None = None) -> dict[str, int]:
    """Build the index at ``db_path`` (from ``specs_dir``, or the bundle).

    Builds into a sibling temp file and moves it into place only after a full
    commit, so a crash never leaves a half-written index at ``db_path``.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(f"{db_path.name}.tmp{os.getpid()}")
    tmp_path.unlink(missing_ok=True)
    sources = _dir_sources(specs_dir) if specs_dir is not None else _bundle_sources()
    conn = sqlite3.connect(tmp_path)
    try:
        counts = _populate(conn, sources)
        if counts["specs"] <= 0 or counts["endpoints"] <= 0:
            raise RuntimeError("no OpenAPI operations found to index")
        conn.commit()
    except Exception:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    conn.close()
    try:
        os.replace(tmp_path, db_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return counts


def _usable(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1 FROM endpoints LIMIT 1").fetchall()
            conn.execute("SELECT 1 FROM fts LIMIT 1").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def _ensure_bundle_index() -> Path:
    path, marker = index_path(), marker_path()
    if path.exists() and marker.exists() and _usable(path):
        return path
    marker.unlink(missing_ok=True)
    build(path)
    marker.write_text("ok\n", encoding="utf-8")
    _remove_old_indexes(path)
    return path


def _remove_old_indexes(current: Path) -> None:
    """Delete indexes left by earlier bundles (tens of MB each); errors are ignored."""
    keep = {current.name, current.name + ".ok"}
    for old in [*current.parent.glob("specs-*.sqlite"), *current.parent.glob("specs-*.sqlite.ok")]:
        if old.name not in keep:
            try:
                old.unlink()
            except OSError:
                pass


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the index read-only.

    With no ``db_path`` the bundle's index is used, built first if it is
    missing, unmarked or unreadable. An explicit ``db_path`` that does not
    exist raises ``FileNotFoundError`` and creates nothing.
    """
    if db_path is None:
        return _open_ro(_ensure_bundle_index())
    if not Path(db_path).exists():
        raise FileNotFoundError(f"no API index at {db_path}")
    return _open_ro(Path(db_path))


# ── Queries ─────────────────────────────────────────────────────────────────


def _fts_escape(q: str) -> str:
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


def _product_filter(product: str | None, alias: str = "") -> tuple[list[str], list[Any]]:
    if not product:
        return [], []
    prefix = f"{alias}." if alias else ""
    return [f"LOWER({prefix}product) = LOWER(?)"], [product]


def _externalize(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data.pop("identity", None)
    data.pop("schema_identity", None)
    return data


def _dedupe(rows: list[sqlite3.Row], *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        data = dict(row)
        key = str(data.get("identity") or f"{data.get('spec_file')}#{data.get('ref') or data.get('path')}")
        if key in seen:
            continue
        seen.add(key)
        out.append(_externalize(data))
        if len(out) >= limit:
            break
    return out


def _query(db_path: Path | None, sql: str, params: list[Any]) -> list[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def search(
    query: str, kind: str | None = None, limit: int = 10, db_path: Path | None = None, *, product: str | None = None
) -> list[dict[str, Any]]:
    """Keyword search across endpoints and schemas (``kind``: endpoint or schema)."""
    sql = (
        "SELECT kind, spec_file, ref, snippet(fts, 3, '', '', '…', 24) AS snippet, product, source_url, identity "
        "FROM fts WHERE fts MATCH ?"
    )
    params: list[Any] = [_fts_escape(query)]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    clauses, extra = _product_filter(product)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(extra)
    sql += " LIMIT ?"
    params.append(limit)
    return _dedupe(_query(db_path, sql, params), limit=limit)


_ENDPOINT_COLUMNS = (
    "product, source_url, identity, spec_name, spec_file, server, method, path, operation_id, summary, description"
)


def _endpoint_rows(where: str, params: list[Any], limit: int, db_path: Path | None, product: str | None):
    sql = f"SELECT {_ENDPOINT_COLUMNS} FROM endpoints WHERE {where}"
    clauses, extra = _product_filter(product)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params = params + extra
    sql += " ORDER BY spec_file, id LIMIT ?"
    return _dedupe(_query(db_path, sql, params + [max(limit * 8, limit)]), limit=limit)


def get_endpoint(
    path_contains: str,
    method: str | None = None,
    limit: int = 10,
    db_path: Path | None = None,
    *,
    product: str | None = None,
) -> list[dict[str, Any]]:
    """Endpoints whose path contains ``path_contains`` (and optional method)."""
    where, params = "path LIKE ?", [f"%{path_contains}%"]
    if method:
        where += " AND method = ?"
        params.append(method.upper())
    return _endpoint_rows(where, params, limit, db_path, product)


def get_exact_endpoint(
    method: str, path: str, limit: int = 10, db_path: Path | None = None, *, product: str | None = None
) -> list[dict[str, Any]]:
    """Endpoint by exact method and OpenAPI path."""
    return _endpoint_rows("method = ? AND path = ?", [method.upper(), path], limit, db_path, product)


def get_endpoint_by_operation_id(
    operation_id: str, limit: int = 10, db_path: Path | None = None, *, product: str | None = None
) -> list[dict[str, Any]]:
    """Endpoint by operationId (case-insensitive)."""
    return _endpoint_rows("operation_id = ? COLLATE NOCASE", [operation_id], limit, db_path, product)


def get_schema(
    name_contains: str, limit: int = 5, db_path: Path | None = None, *, product: str | None = None
) -> list[dict[str, Any]]:
    """Schemas whose name contains ``name_contains``, each with its fields."""
    conn = connect(db_path)
    try:
        sql = "SELECT product, source_url, identity, spec_name, spec_file, name, description FROM schemas WHERE name LIKE ?"
        params: list[Any] = [f"%{name_contains}%"]
        clauses, extra = _product_filter(product)
        if clauses:
            sql += " AND " + " AND ".join(clauses)
            params.extend(extra)
        sql += " ORDER BY spec_file, id LIMIT ?"
        params.append(max(limit * 8, limit))
        out = []
        seen: set[str] = set()
        for s in conn.execute(sql, params).fetchall():
            if s["identity"] in seen:
                continue
            seen.add(s["identity"])
            fields = conn.execute(
                "SELECT field_name, path, type, description, enums, enum_descriptions FROM fields "
                "WHERE schema_identity = ?",
                (s["identity"],),
            ).fetchall()
            row = _externalize(s)
            row["fields"] = [
                {
                    **dict(f),
                    "enums": json.loads(f["enums"]) if f["enums"] else None,
                    "enum_descriptions": json.loads(f["enum_descriptions"]) if f["enum_descriptions"] else None,
                }
                for f in fields
            ]
            out.append(row)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def get_enum(
    field_name: str,
    schema_contains: str | None = None,
    limit: int = 10,
    db_path: Path | None = None,
    *,
    product: str | None = None,
) -> list[dict[str, Any]]:
    """The allowed values of a field, across all specs."""
    sql = (
        "SELECT product, source_url, schema_identity, spec_name, spec_file, schema_name, field_name, path, "
        "type, description, enums, enum_descriptions FROM fields WHERE field_name = ? AND enums IS NOT NULL"
    )
    params: list[Any] = [field_name]
    if schema_contains:
        sql += " AND schema_name LIKE ?"
        params.append(f"%{schema_contains}%")
    clauses, extra = _product_filter(product)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
        params.extend(extra)
    sql += " ORDER BY spec_file, id LIMIT ?"
    params.append(max(limit * 8, limit))
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for r in _query(db_path, sql, params):
        key = (str(r["schema_identity"]), str(r["path"]), r["enums"], r["enum_descriptions"])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                **_externalize(r),
                "enums": json.loads(r["enums"]),
                "enum_descriptions": json.loads(r["enum_descriptions"]) if r["enum_descriptions"] else None,
            }
        )
        if len(out) >= limit:
            break
    return out


def get_response_description(
    product: str, status_code: int | str, min_share: float = 0.6, db_path: Path | None = None
) -> str | None:
    """The API's own meaning of a status code for a product, when one meaning dominates.

    Never raises: a missing or unreadable index gives ``None``.
    """
    if not product:
        return None
    try:
        rows = _query(
            db_path,
            "SELECT description, COUNT(*) AS c FROM ("
            "  SELECT DISTINCT method, path, status_code, description FROM responses"
            "  WHERE product = ? AND status_code = ? AND description IS NOT NULL AND description != ''"
            ") GROUP BY description ORDER BY c DESC",
            [product, str(status_code)],
        )
    except (FileNotFoundError, sqlite3.Error, RuntimeError, OSError):
        return None
    if not rows:
        return None
    total = sum(r["c"] for r in rows)
    if total and rows[0]["c"] / total >= min_share:
        return str(rows[0]["description"])
    return None


# ── Natural-language lookup (backs the lookup_api tool) ─────────────────────

_STOPWORDS = frozenset(
    """
a an the is are was were be been being do does did can could should would will
what which who whose when where why how there here this that these those it its
i you we they my your of for to in on at by with from into over under about as
and or not no if then than but exist exists existing available use used uses
using new central api apis valid value values field fields enum enums key keys
required need needed needs http https method methods url uri endpoint endpoints
response request list lists get read set sets type types kind name names
configure configures configured configuration config
accept accepts allow allows allowed support supports supported
""".split()  # noqa: SIM905 - a word list reads best as text
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-._]*")
_EXACT_ENDPOINT_RE = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s?#]*)\s*$", re.IGNORECASE)
_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{1,255}$")
_LOOKUP_CACHE_MAX = 128
_lookup_cache: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()

# Specs say "wlan"/"essid" where people say "SSID". Synonyms join the same
# concept group, so they corroborate a match without counting twice.
_SYNONYMS: dict[str, list[str]] = {
    "ssid": ["wlan", "essid"],
    "wlan": ["ssid", "essid"],
    "gw": ["gateway"],
    "gateway": ["gw"],
}


def _stem_variants(word: str) -> list[str]:
    if word.endswith("ies") and len(word) > 4:
        return [word[:-3] + "y", word[:-1]]
    if word.endswith("s") and len(word) > 3:
        return [word[:-1]]
    return [word]


def _query_groups(query: str) -> list[list[str]]:
    """Concept groups of lightly stemmed terms; one group per query word."""
    groups: list[list[str]] = []
    seen_tokens: set[str] = set()
    for tok in _TOKEN_RE.findall(query.lower()):
        tok = tok.strip("-._")
        if not tok or tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        stems: list[str] = []
        for part in [tok] + (tok.split("-") if "-" in tok else []):
            if part in _STOPWORDS:
                continue
            for v in _stem_variants(part):
                if len(v) < 3 or v.isdigit() or v in _STOPWORDS or v in stems:
                    continue
                stems.append(v)
        for s in list(stems):
            for syn in _SYNONYMS.get(s, []):
                if syn not in stems:
                    stems.append(syn)
        if stems:
            groups.append(stems)
    return groups


def _fmt_endpoint(row: dict[str, Any]) -> str:
    url = f"{row['server']}{row['path']}" if row.get("server") else row["path"]
    desc = (row.get("description") or "")[:300]
    return f"{row['method']} {url} — {row.get('summary', '')} {desc}".strip()


def _hit(row: dict[str, Any], *, kind: str, ref: str, text: str, score: int) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "text": text,
        "source": "specs",
        "file_path": f"specs/{row['spec_file']}#{ref}",
        "kind": kind,
        "score": score,
        "product": row.get("product"),
    }
    if kind == "endpoint":
        method, _, path = ref.partition(" ")
        hit["method"], hit["path"] = method, path
    return hit


def _fmt_enum_field(row: dict[str, Any]) -> str:
    enums = row.get("enums") or []
    return (
        f"{row['schema_name']}.{row['path']} ({row['spec_name']}) type={row.get('type', '')}: "
        f"{(row.get('description') or '')[:200]} Enum: {', '.join(map(str, enums[:24]))}"
    ).strip()


def clear_lookup_cache() -> None:
    _lookup_cache.clear()


def lookup(
    query: str, top_k: int = 10, product: str | None = None, *, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Exact API lookup for a question, ``METHOD /path`` or operationId.

    Returns ``[]`` when the specs hold no confident answer. Raises
    ``FileNotFoundError`` when an explicit ``db_path`` is missing or unreadable.
    """
    top_k = max(1, min(20, top_k))
    if db_path is None:
        path = _ensure_bundle_index()
    else:
        path = Path(db_path)
        if not path.exists():
            raise FileNotFoundError(f"no API index at {path}")
    stat = path.stat()
    key = (str(path), query.strip(), top_k, product, stat.st_mtime_ns, stat.st_size)
    cached = _lookup_cache.get(key)
    if cached is not None:
        _lookup_cache.move_to_end(key)
        return [dict(h) for h in cached]
    try:
        hits = _lookup(query, top_k, path, product)
    except sqlite3.Error as exc:
        if db_path is None:
            marker_path().unlink(missing_ok=True)
            hits = _lookup(query, top_k, _ensure_bundle_index(), product)
        else:
            raise FileNotFoundError(f"API index at {path} is unreadable ({exc})") from exc
    _lookup_cache[key] = [dict(h) for h in hits]
    while len(_lookup_cache) > _LOOKUP_CACHE_MAX:
        _lookup_cache.popitem(last=False)
    return [dict(h) for h in hits]


def _lookup(query: str, top_k: int, db_path: Path, product: str | None) -> list[dict[str, Any]]:
    stripped = query.strip()
    exact = _EXACT_ENDPOINT_RE.fullmatch(stripped)
    if exact:
        method, path = exact.groups()
        rows = get_exact_endpoint(method, path, limit=top_k, db_path=db_path, product=product)
        return [
            _hit(r, kind="endpoint", ref=f"{r['method']} {r['path']}", text=_fmt_endpoint(r), score=100) for r in rows
        ]
    if _OPERATION_ID_RE.fullmatch(stripped):
        rows = get_endpoint_by_operation_id(stripped, limit=top_k, db_path=db_path, product=product)
        if rows:
            return [
                _hit(r, kind="endpoint", ref=f"{r['method']} {r['path']}", text=_fmt_endpoint(r), score=100)
                for r in rows
            ]

    groups = _query_groups(query)
    if not groups:
        return []
    n_terms = len(groups)
    threshold = 1 if n_terms == 1 else (2 if n_terms <= 3 else 3)
    flat: list[str] = []
    for g in groups:
        for s in g:
            if s not in flat:
                flat.append(s)

    def _present(stem: str, low: str) -> bool:
        # Short stems need a whole word ("mac" is inside "machine").
        if len(stem) <= 3:
            return bool(re.search(rf"\b{re.escape(stem)}\b", low))
        return stem in low

    def matched(blob: str) -> int:
        low = blob.lower()
        return sum(1 for g in groups if any(_present(s, low) for s in g))

    hits: dict[str, dict[str, Any]] = {}

    def add(row: dict[str, Any], *, kind: str, ref: str, text: str, score: int, exact: bool, key: str) -> None:
        cur = hits.get(key)
        if cur is None:
            hit = _hit(row, kind=kind, ref=ref, text=text, score=score)
            hit["_exact"] = exact
            hits[key] = hit
        else:
            cur["score"] = max(cur["score"], score)
            cur["_exact"] = cur["_exact"] or exact

    # 1. Exact enum/field matches for field-like terms.
    for term in flat:
        if len(term) < 4:
            continue
        for row in get_enum(term, limit=16, db_path=db_path, product=product):
            blob = (
                f"{row['spec_file']} {row['schema_name']} {row['path']} {row.get('description') or ''} "
                f"{' '.join(map(str, row.get('enums') or []))} {row.get('enum_descriptions') or ''}"
            )
            ref = f"{row['schema_name']}.{row['path']}"
            add(
                row,
                kind="enum",
                ref=ref,
                text=_fmt_enum_field(row),
                score=matched(blob),
                exact=True,
                key=f"enum:{row['product']}:{row['spec_file']}#{ref}",
            )

    # 2. Exact endpoint matches for hyphenated tokens (one right-trim).
    for term in (g[0] for g in groups if "-" in g[0]):
        for candidate in (term, term.rsplit("-", 1)[0]):
            if "-" not in candidate:
                continue
            rows = get_endpoint(candidate, limit=6, db_path=db_path, product=product)
            if rows:
                for row in rows:
                    blob = (
                        f"{row['spec_file']} {row['method']} {row['path']} "
                        f"{row.get('summary') or ''} {row.get('description') or ''}"
                    )
                    add(
                        row,
                        kind="endpoint",
                        ref=f"{row['method']} {row['path']}",
                        text=_fmt_endpoint(row),
                        score=matched(blob),
                        exact=True,
                        key=f"{row['product']}:{row['method']} {row['path']}",
                    )
                break

    # 3. Full-text prefix search, re-ranked by how many query concepts a row holds.
    conn = connect(db_path)
    try:
        sql = "SELECT kind, spec_file, ref, body, product, source_url, identity FROM fts WHERE fts MATCH ?"
        params: list[Any] = [" OR ".join(f'"{s}"*' for s in flat)]
        clauses, extra = _product_filter(product)
        if clauses:
            sql += " AND " + " AND ".join(clauses)
            params.extend(extra)
        sql += " ORDER BY bm25(fts) LIMIT 400"
        for r in conn.execute(sql, params).fetchall():
            score = matched(f"{r['spec_file']} {r['body']}")
            if score < threshold:
                continue
            row = dict(r)
            if r["kind"] == "endpoint":
                method, _, path = r["ref"].partition(" ")
                ep = conn.execute(
                    f"SELECT {_ENDPOINT_COLUMNS} FROM endpoints WHERE identity = ? LIMIT 1", (r["identity"],)
                ).fetchone()
                text = _fmt_endpoint(dict(ep)) if ep else r["ref"]
                key = str(r["identity"])
            else:
                fields = conn.execute(
                    "SELECT field_name, path, type, description, enums FROM fields WHERE schema_identity = ? LIMIT 400",
                    (r["identity"],),
                ).fetchall()
                parts = []
                for f in fields:
                    enums = json.loads(f["enums"]) if f["enums"] else []
                    if matched(f"{f['field_name']} {f['description'] or ''} {' '.join(map(str, enums))}"):
                        sfx = f" enum: {', '.join(map(str, enums[:24]))}" if enums else ""
                        parts.append(f"{f['path']} ({f['type']}){sfx}")
                    if len(parts) >= 8:
                        break
                text = f"Schema {r['ref']} [{r['spec_file']}]: " + "; ".join(parts)
                key = str(r["identity"])
            add(row, kind=r["kind"], ref=r["ref"], text=text, score=score, exact=False, key=key)
            if r["kind"] == "endpoint":
                # Tie-break: an endpoint whose own path names the concepts
                # beats a schema that only mentions them.
                hits[key]["_path_score"] = matched(r["ref"])
    finally:
        conn.close()

    # Exact hits first, then by concept coverage. An exact hit still needs one
    # corroborating concept beyond the name that matched it.
    exact_floor = min(2, n_terms)
    out = [h for h in hits.values() if (h["_exact"] and h["score"] >= exact_floor) or h["score"] >= threshold]
    out.sort(key=lambda h: (not h["_exact"], -h["score"], -h.get("_path_score", 0)))
    for h in out:
        h.pop("_exact")
        h.pop("_path_score", None)
    return out[:top_k]
