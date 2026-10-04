import hashlib
import json
import pathlib
from importlib.resources import files

from casper_network_mcp import specs_bundle

# Section A's licence and redistribution sentences, word for word from the
# source NOTICE (hpe-networking-mcp vendor/openapi/NOTICE.md). Compared with
# line breaks folded to single spaces and bold marks removed.
HPE_LICENCE_SENTENCES = [
    "These 30 documents are proprietary HPE Aruba Networking material.",
    "They are not open source, and this repository's MIT licence does not extend to them.",
    (
        "HPE publishes them without an accompanying licence grant and without an authentication barrier, as the "
        "machine-readable form of public API reference documentation whose entire purpose is to be consumed by API "
        "clients."
    ),
    (
        "They are redistributed here verbatim, with attribution and provenance, so that `lookup_api` answers exact "
        "API questions from a clean clone with no network access — the same use the publisher intends, moved offline."
    ),
    "This is a good-faith reliance on published-for-integration intent, not a licence.",
    "No warranty and no endorsement by HPE is claimed or implied.",
    (
        '"HPE", "Aruba", "Aruba Networking" and "New Central" are marks of Hewlett Packard Enterprise, used here only '
        "to identify the API being described."
    ),
    "If HPE asks for these documents to be removed, remove them.",
    "Downstream users redistributing this repository inherit that position and should make their own assessment.",
]

ROOT = files("casper_network_mcp") / "specs"


def _folded(text: str) -> str:
    return " ".join(text.replace("**", "").split())


def _manifest():
    return json.loads((ROOT / "MANIFEST.json").read_text())


def test_every_bundled_spec_matches_its_manifest_hash():
    manifest = _manifest()
    assert manifest["schema_version"] == 2
    assert manifest["documents"]
    for doc in manifest["documents"]:
        assert set(doc) <= {
            "path",
            "source_url",
            "reference_page",
            "sha256",
            "fetched",
            "license",
            "path_count",
            "upstream_commit",
            "title",
        }
        data = (ROOT / doc["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == doc["sha256"], doc["path"]
        assert doc["source_url"].startswith(
            ("https://dash.readme.com/api/v1/api-registry/", "https://raw.githubusercontent.com/mistsys/mist_openapi/")
        )
        if doc["source_url"].startswith("https://raw.githubusercontent.com/"):
            assert doc["upstream_commit"] in doc["source_url"]
            assert len(doc["upstream_commit"]) == 40
        assert len(json.loads(data)["paths"]) == doc["path_count"]
    assert "ingestion" not in json.dumps(manifest)
    on_disk = {p.name for p in ROOT.iterdir() if p.name.endswith(".json") and p.name != "MANIFEST.json"}
    assert on_disk == {doc["path"] for doc in manifest["documents"]}


def test_notice_keeps_hpe_licence_wording_and_no_ingestion_paths():
    text = (ROOT / "NOTICE.md").read_text()
    assert "proprietary" in text.lower() and "ingestion" not in text and "scrape" not in text.lower()
    folded = _folded(text)
    for sentence in HPE_LICENCE_SENTENCES:
        assert sentence in folded, sentence
    central = specs_bundle.documents("central")
    assert f"{len(central)} New Central documents" in text
    assert f"{sum(d['path_count'] for d in central)} API paths" in text


def test_notice_has_the_mist_licence_at_the_pinned_commit():
    text = (ROOT / "NOTICE.md").read_text()
    mist = next(d for d in _manifest()["documents"] if d["path"] == "mist.openapi.json")
    assert mist["upstream_commit"] in text
    assert "Permission is hereby granted, free of charge" in text
    assert "{" + "MIST" not in text


def test_both_products_have_operations():
    assert len(specs_bundle.operations("mist")) > 500
    assert len(specs_bundle.operations("central")) > 300


def test_operation_shape_and_query_params():
    ops = {(o.method, o.path): o for o in specs_bundle.operations("mist")}
    op = ops[("GET", "/api/v1/sites/{site_id}/wlans")]
    assert op.operation_id and isinstance(op.query_params, tuple) and isinstance(op.enums, dict)
    assert all(o.method in {"GET", "POST", "PUT", "PATCH", "DELETE"} for o in ops.values())
    sle = [o for o in ops.values() if o.path.endswith("/sle/{scope}/{scope_id}/metric/{metric}/summary")]
    assert sle and "site" in sle[0].enums.get("scope", ())


def test_unknown_product_is_empty():
    assert specs_bundle.operations("nope") == []


def test_product_for():
    assert (
        specs_bundle.product_for(
            {"path": "mist.openapi.json", "source_url": "https://raw.githubusercontent.com/mistsys/x"}
        )
        == "mist"
    )
    assert (
        specs_bundle.product_for({"path": "clearpass-api.json", "source_url": "https://dash.readme.com/x"})
        == "clearpass"
    )
    assert specs_bundle.product_for({"path": "wireless-x.json", "source_url": "https://dash.readme.com/x"}) == "central"


def test_spec_pins_match_the_manifest():
    pins = json.loads((pathlib.Path(__file__).resolve().parents[1] / "scripts" / "spec_pins.json").read_text())
    by_path = {d["path"]: d for d in _manifest()["documents"]}
    for pin in pins["central"]:
        if pin["path"] in by_path:
            assert by_path[pin["path"]]["source_url"].endswith("/" + pin["registry_id"])
    assert by_path["mist.openapi.json"]["upstream_commit"] == pins["mist"][0]["commit"]


def test_refresh_script_is_honest():
    text = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "refresh_specs.py").read_text()
    assert "casper-network-mcp-refresh/" in text
    for word in ("Mozilla", "Safari", "Chrome/", "Cookie", "cookies=", "ingestion"):
        assert word not in text, word
