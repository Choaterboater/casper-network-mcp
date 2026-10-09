"""Fetch the vendor OpenAPI documents this package bundles, honestly.

Central and ClearPass documents come straight from HPE's developer-portal API
registry (the portal runs on ReadMe):
``https://dash.readme.com/api/v1/api-registry/<id>``.
The Mist document comes from an exact commit of ``mistsys/mist_openapi`` on
GitHub. Every request names this project in its User-Agent; there are no
browser headers, no cookies and no HTML page fetches.

Each file is stored as the bytes received, so its sha256 in MANIFEST.json is
the hash of both what was fetched and what is on disk. A document the
registry refuses (an error status or a body that is not an OpenAPI document)
is left out and listed at the end of the run.

    python scripts/refresh_specs.py            # fetch every pin, rewrite specs/
    python scripts/refresh_specs.py --check    # fetch and compare hashes, write nothing
    python scripts/refresh_specs.py --update [--mist-commit SHA]
                                               # re-pin (optionally a new Mist commit),
                                               # rewrite specs/ and print what changed

Inputs: scripts/spec_pins.json. Output: src/casper_network_mcp/specs/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from casper_network_mcp import __version__

PINS_PATH = ROOT / "scripts" / "spec_pins.json"
SPECS_DIR = ROOT / "src" / "casper_network_mcp" / "specs"
REGISTRY_URL = "https://dash.readme.com/api/v1/api-registry/{registry_id}"
MIST_URL = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"
USER_AGENT = f"casper-network-mcp-refresh/{__version__} (+https://github.com/Choaterboater/casper-network-mcp)"
CENTRAL_LICENSE = "Proprietary HPE Aruba Networking API documentation (not MIT); see NOTICE.md"
CLEARPASS_LICENSE = "Proprietary HPE Aruba Networking ClearPass API documentation (not MIT); see NOTICE.md"
_SAFE_FILE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.json$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REGISTRY_ID = re.compile(r"^[a-z0-9]{6,40}$")


class Refused(Exception):
    """The upstream did not return an OpenAPI document."""


def load_pins() -> dict[str, Any]:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def planned_documents(pins: dict[str, Any]) -> list[dict[str, Any]]:
    """Every document to fetch: path, title, url, and the manifest extras."""
    out: list[dict[str, Any]] = []
    for product, licence in (("central", CENTRAL_LICENSE), ("clearpass", CLEARPASS_LICENSE)):
        for pin in pins.get(product, []):
            if not _SAFE_FILE.match(pin["path"]) or not _REGISTRY_ID.match(pin["registry_id"]):
                raise SystemExit(f"bad {product} pin: {pin}")
            # The package tells ClearPass documents apart by this file-name prefix.
            if (product == "clearpass") != pin["path"].startswith("clearpass-"):
                raise SystemExit(
                    f"{product} pin file names must {'' if product == 'clearpass' else 'not '}"
                    f"start with clearpass-: {pin['path']}"
                )
            entry = {
                "path": pin["path"],
                "title": pin["title"],
                "source_url": REGISTRY_URL.format(registry_id=pin["registry_id"]),
                "license": licence,
            }
            if pin.get("reference_page"):
                entry["reference_page"] = pin["reference_page"]
            out.append(entry)
    for pin in pins.get("mist", []):
        if not _SAFE_FILE.match(pin["path"]) or not _SHA.match(pin["commit"]):
            raise SystemExit(f"bad Mist pin (needs a 40-character commit): {pin}")
        out.append(
            {
                "path": pin["path"],
                "title": pin["title"],
                "source_url": MIST_URL.format(repo=pin["repo"], commit=pin["commit"], path=pin["path"]),
                "license": "MIT",
                "upstream_commit": pin["commit"],
            }
        )
    return out


def fetch(client: httpx.Client, url: str) -> bytes:
    resp = client.get(url)
    if resp.status_code != 200:
        raise Refused(f"HTTP {resp.status_code}")
    data = resp.content
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise Refused("not JSON") from exc
    if not isinstance(doc, dict) or not (doc.get("openapi") or doc.get("swagger")):
        raise Refused("not an OpenAPI document")
    if not isinstance(doc.get("paths"), dict) or not doc["paths"]:
        raise Refused("no paths")
    return data


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=False,
    )


def fetch_all(documents: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], bytes]], list[str]]:
    got: list[tuple[dict[str, Any], bytes]] = []
    dropped: list[str] = []
    today = dt.datetime.now(dt.UTC).date().isoformat()
    with make_client() as client:
        for doc in documents:
            try:
                data = fetch(client, doc["source_url"])
            except (Refused, httpx.HTTPError) as exc:
                dropped.append(f"{doc['path']} ({doc['source_url']}): {exc}")
                continue
            entry = dict(doc)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["fetched"] = today
            entry["path_count"] = len(json.loads(data)["paths"])
            got.append((entry, data))
            print(f"  ok   {doc['path']}  {entry['path_count']} paths", file=sys.stderr)
    return got, dropped


def _ordered(entry: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "path",
        "title",
        "source_url",
        "reference_page",
        "sha256",
        "fetched",
        "license",
        "path_count",
        "upstream_commit",
    )
    return {k: entry[k] for k in keys if k in entry}


def write_bundle(got: list[tuple[dict[str, Any], bytes]]) -> None:
    """Write every file and MANIFEST.json to a temp dir, then swap them in.

    Old JSON documents not in the new set are removed; MANIFEST.json is moved
    in last, so an interrupted run leaves either the old manifest (and the
    package's hash test fails loudly) or the complete new set.
    """
    manifest = {"schema_version": 2, "documents": [_ordered(entry) for entry, _ in got]}
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SPECS_DIR.parent, prefix=".specs-new-") as tmp:
        tmp_dir = Path(tmp)
        for entry, data in got:
            (tmp_dir / entry["path"]).write_bytes(data)
        (tmp_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        new_names = {entry["path"] for entry, _ in got}
        for old in SPECS_DIR.glob("*.json"):
            if old.name != "MANIFEST.json" and old.name not in new_names:
                old.unlink()
        for name in sorted(new_names):
            os.replace(tmp_dir / name, SPECS_DIR / name)
        os.replace(tmp_dir / "MANIFEST.json", SPECS_DIR / "MANIFEST.json")


def current_manifest() -> dict[str, dict[str, Any]]:
    path = SPECS_DIR / "MANIFEST.json"
    if not path.exists():
        return {}
    return {d["path"]: d for d in json.loads(path.read_text(encoding="utf-8"))["documents"]}


def print_diff(old: dict[str, dict[str, Any]], got: list[tuple[dict[str, Any], bytes]]) -> None:
    new = {entry["path"]: entry for entry, _ in got}
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before is None:
            count = after["path_count"] if after is not None else 0
            print(f"added    {name} ({count} paths)")
        elif after is None:
            print(f"removed  {name}")
        elif before["sha256"] != after["sha256"]:
            print(f"changed  {name}: {before['path_count']} -> {after['path_count']} paths")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fetch and compare hashes; write nothing")
    mode.add_argument("--update", action="store_true", help="re-pin and print what changed")
    ap.add_argument("--mist-commit", help="with --update: pin this mistsys/mist_openapi commit")
    args = ap.parse_args(argv)

    pins = load_pins()
    if args.mist_commit:
        if not args.update:
            ap.error("--mist-commit needs --update")
        if not _SHA.match(args.mist_commit):
            ap.error("--mist-commit must be a 40-character commit")
        for pin in pins.get("mist", []):
            pin["commit"] = args.mist_commit

    documents = planned_documents(pins)
    print(f"fetching {len(documents)} documents as {USER_AGENT}", file=sys.stderr)
    got, dropped = fetch_all(documents)

    if args.check:
        old = current_manifest()
        bad = [
            entry["path"]
            for entry, _ in got
            if entry["path"] not in old or old[entry["path"]]["sha256"] != entry["sha256"]
        ]
        missing = sorted(set(old) - {entry["path"] for entry, _ in got})
        for name in bad:
            print(f"differs  {name}")
        for name in missing:
            print(f"not fetched  {name}")
        for line in dropped:
            print(f"refused  {line}")
        ok = not bad and not missing
        print("every hash reproduced" if ok else "hashes differ", file=sys.stderr)
        return 0 if ok else 1

    if not got:
        print("nothing fetched; specs/ left as it was", file=sys.stderr)
        return 1
    old = current_manifest()
    write_bundle(got)
    if args.update:
        PINS_PATH.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
        print_diff(old, got)
    for line in dropped:
        print(f"refused and left out: {line}")
    print(f"wrote {len(got)} documents to {SPECS_DIR.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
