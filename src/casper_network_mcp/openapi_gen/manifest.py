"""Build and load the generated-operation manifest for each product.

Adapted from hpe-networking-mcp ``openapi_gen/manifest.py``. A manifest is one JSON file per product
under ``openapi_gen/manifests/``, built by ``scripts/build_manifests.py``
from the bundled ``specs/`` documents only: each operation's name, method,
path, summary, parameters and request body. Nothing else feeds it.

There is no capability field here: each generated tool's change kind is
worked out from its method and path when the tool is registered
(``core/kinds.py``), so the rules and the hand-checked lists live in one place.
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from casper_network_mcp import specs_bundle
from casper_network_mcp.openapi_gen.ir import SpecParser
from casper_network_mcp.openapi_gen.naming import NameAllocator

__all__ = [
    "SCHEMA_VERSION",
    "Manifest",
    "ManifestTool",
    "build_merged_manifest",
    "build_product_manifest",
    "dumps",
    "load_manifest",
    "sha256_bytes",
]

SCHEMA_VERSION = 3


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ManifestTool:
    name: str
    method: str
    path: str
    operation_id: str


@dataclass(frozen=True)
class Manifest:
    product: str
    base_path: str
    source: dict[str, Any]
    operations: list[dict[str, Any]] = field(repr=False)

    @property
    def tools(self) -> list[ManifestTool]:
        return [
            ManifestTool(op["name"], op["method"], op["path"], str(op.get("operation_id") or ""))
            for op in self.operations
        ]


def _merge_duplicate_record(existing: dict[str, Any], op: Any) -> None:
    """Merge parameters and request metadata from a duplicate method/path."""
    parameters = existing.setdefault("parameters", [])
    by_key = {(item.get("in"), item.get("name")): item for item in parameters}
    for parameter in (item.to_dict() for item in op.parameters):
        key = (parameter.get("in"), parameter.get("name"))
        current = by_key.get(key)
        if current is None:
            parameters.append(parameter)
            by_key[key] = parameter
            continue
        if parameter.get("required"):
            current["required"] = True
        if not current.get("description") and parameter.get("description"):
            current["description"] = parameter["description"]
        if not current.get("enum") and parameter.get("enum"):
            current["enum"] = parameter["enum"]
        for metadata_key in ("format", "style", "explode"):
            if metadata_key not in current and metadata_key in parameter:
                current[metadata_key] = parameter[metadata_key]

    if op.request_body is not None:
        incoming = op.request_body.to_dict()
        current_body = existing.get("request_body")
        if current_body is None:
            existing["request_body"] = incoming
        else:
            current_body["required"] = bool(current_body.get("required") or incoming.get("required"))
            current_properties = list(current_body.get("properties") or [])
            for name in incoming.get("properties") or []:
                if name not in current_properties:
                    current_properties.append(name)
            if current_properties:
                current_body["properties"] = current_properties
            required_properties = list(current_body.get("required_properties") or [])
            for name in incoming.get("required_properties") or []:
                if name not in required_properties:
                    required_properties.append(name)
            if required_properties:
                current_body["required_properties"] = required_properties
            property_formats = dict(current_body.get("property_formats") or {})
            property_formats.update(incoming.get("property_formats") or {})
            if property_formats:
                current_body["property_formats"] = dict(sorted(property_formats.items()))

    if op.tags:
        tags = list(existing.get("tags") or [])
        for tag in op.tags:
            if tag not in tags:
                tags.append(tag)
        existing["tags"] = tags
    if not existing.get("summary") and op.summary:
        existing["summary"] = op.summary
    if not existing.get("description") and op.description:
        existing["description"] = op.description
    if op.deprecated:
        existing["deprecated"] = True
    if op.sunset and not existing.get("sunset"):
        existing["sunset"] = op.sunset


def _record(name: str, op: Any, source_file: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "key": op.key,
        "method": op.method,
        "path": op.path,
        "source_file": source_file,
    }
    if op.operation_id:
        record["operation_id"] = op.operation_id
    if op.summary:
        record["summary"] = op.summary
    if op.description:
        record["description"] = op.description
    if op.tags:
        record["tags"] = op.tags
    record["parameters"] = [p.to_dict() for p in op.parameters]
    if op.request_body is not None:
        record["request_body"] = op.request_body.to_dict()
    if op.deprecated:
        record["deprecated"] = True
    if op.sunset:
        record["sunset"] = op.sunset
    return record


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def build_merged_manifest(
    documents: list[tuple[str, str, dict[str, Any]]],
    *,
    platform: str,
    base_path: str = "",
) -> dict[str, Any]:
    """Build one deterministic manifest from several parsed documents.

    ``documents`` entries are ``(source_file, source_sha256, parsed_spec)``.
    An operation (method and path) found in more than one document is kept
    once, from the first file by name, with the others' parameters merged in.
    Only GET, POST, PUT, PATCH and DELETE operations become tools.
    """
    allocator = NameAllocator()
    records: list[dict[str, Any]] = []
    records_by_key: dict[str, dict[str, Any]] = {}
    seen_operations: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []

    for source_file, source_sha256, spec in sorted(documents, key=lambda item: item[0]):
        parser = SpecParser(spec)
        operations = [op for op in parser.operations() if op.method in _METHODS]
        info = spec.get("info", {}) if isinstance(spec.get("info"), dict) else {}
        sources.append(
            {
                "file": source_file,
                "sha256": source_sha256,
                "openapi": parser.version,
                "title": info.get("title", ""),
                "version": info.get("version", ""),
                "operation_count": len(operations),
            }
        )
        for op in operations:
            if op.key in seen_operations:
                _merge_duplicate_record(records_by_key[op.key], op)
                duplicates.append(
                    {"key": op.key, "kept_source": seen_operations[op.key], "duplicate_source": source_file}
                )
                continue
            seen_operations[op.key] = source_file
            name = allocator.allocate(platform, op.method, op.path, op.operation_id)
            record = _record(name, op, source_file)
            records.append(record)
            records_by_key[op.key] = record

    digest_input = "\n".join(f"{source['file']}:{source['sha256']}" for source in sources)
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "base_path": base_path,
        "source": {
            "file_count": len(sources),
            "sha256": sha256_bytes(digest_input.encode()),
            "operation_count": len(records),
            "duplicate_operation_count": len(duplicates),
            "files": sources,
        },
        "duplicate_operations": duplicates,
        "operations": records,
    }


def _base_path(specs: list[dict[str, Any]]) -> str:
    """The URL path every server in the documents shares (ClearPass: ``/api``)."""
    paths: set[str] = set()
    for spec in specs:
        for server in spec.get("servers") or []:
            if isinstance(server, dict) and isinstance(server.get("url"), str):
                paths.add(urlsplit(server["url"]).path.rstrip("/"))
    if len(paths) > 1:
        raise ValueError(f"the documents disagree on their base path: {sorted(paths)}")
    return paths.pop() if paths else ""


def build_product_manifest(product: str) -> dict[str, Any]:
    """Build ``product``'s manifest from the bundled spec documents only."""
    docs = specs_bundle.documents(product)
    if not docs:
        raise ValueError(f"no bundled documents for {product!r}")
    parsed = [(doc["path"], doc["sha256"], specs_bundle.load_document(doc["path"])) for doc in docs]
    return build_merged_manifest(parsed, platform=product, base_path=_base_path([spec for _, _, spec in parsed]))


def dumps(manifest: dict[str, Any]) -> str:
    """Serialise a manifest deterministically."""
    return json.dumps(manifest, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


@functools.cache
def load_manifest(product: str) -> Manifest:
    """The committed manifest for ``product``; ``FileNotFoundError`` if there is none."""
    resource = files("casper_network_mcp") / "openapi_gen" / "manifests" / f"{product}.json"
    if not resource.is_file():
        raise FileNotFoundError(f"no generated manifest for {product!r}")
    data = json.loads(resource.read_text(encoding="utf-8"))
    return Manifest(
        product=data["platform"],
        base_path=data.get("base_path", ""),
        source=data["source"],
        operations=data["operations"],
    )
