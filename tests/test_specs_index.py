"""The API lookup index over the bundled specs.

Lookup cases ported from hpe-networking-mcp ``tests/unit/test_specs_index_lookup.py``,
``test_build_spec_index.py``, ``test_specs_index_responses.py`` and
``test_spec_index_degradation.py``.
The index is built from the package's own specs into a cache file on first
use, so a missing index means "rebuild it", not "run a build command".
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from casper_network_mcp import specs_index

FIXTURE_SPECS = {
    "cda-auth-profile.json": {
        "info": {"title": "CDA Auth Profile"},
        "paths": {
            "/auth-profiles/{name}": {
                "patch": {
                    "operationId": "updateAuthProfile",
                    "summary": "Update auth profile",
                    "description": "Update an existing CDA auth profile.",
                    "responses": {"429": {"$ref": "#/components/responses/TooMany"}},
                },
            },
        },
        "components": {
            "responses": {"TooMany": {"description": "Too many requests"}},
            "schemas": {
                "CdaAuthProfile": {
                    "description": "CDA authentication profile.",
                    "properties": {
                        "auth-type": {
                            "type": "string",
                            "description": "Authentication type for the profile.",
                            "enum": ["MPSK", "EAP", "CAPTIVE_PORTAL", "MAB"],
                        },
                    },
                },
            },
        },
    },
    "firmware-management.json": {
        "info": {"title": "Firmware Management"},
        "paths": {
            "/device-firmware": {
                "post": {
                    "operationId": "createDeviceFirmware",
                    "summary": "Create device firmware settings",
                    "description": "Configure device firmware for a scope.",
                    "responses": {"429": {"description": "Too many requests"}},
                },
                "patch": {
                    "operationId": "updateDeviceFirmware",
                    "summary": "Update device firmware settings",
                    "description": "Update device firmware for a scope.",
                    "responses": {"429": {"description": "Too many requests"}, "404": {"description": "Not here"}},
                },
            },
        },
        "components": {"schemas": {}},
    },
    "interface-ethernet.json": {
        "info": {"title": "Interface Ethernet"},
        "paths": {},
        "components": {
            "schemas": {
                "MvrpInterfaceConfig": {
                    "description": "MVRP interface settings.",
                    "properties": {
                        "registration": {
                            "type": "string",
                            "description": "MVRP registrar state machine control.",
                            "enum": ["NORMAL", "FIXED", "FORBIDDEN"],
                        },
                    },
                },
            }
        },
    },
}


@pytest.fixture
def db(tmp_path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for fname, spec in FIXTURE_SPECS.items():
        (specs_dir / fname).write_text(json.dumps(spec))
    db_path = tmp_path / "specs.sqlite"
    counts = specs_index.build(db_path, specs_dir=specs_dir)
    assert counts["specs"] == 3 and counts["endpoints"] == 3
    specs_index.clear_lookup_cache()
    return db_path


@pytest.fixture(scope="module")
def shared_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("shared-cache")


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    monkeypatch.setattr(specs_index, "cache_dir", lambda: target)
    specs_index.clear_lookup_cache()
    return target


@pytest.fixture
def built_cache(shared_cache, monkeypatch):
    """One bundle build shared by the read-only tests (the build takes a few seconds)."""
    monkeypatch.setattr(specs_index, "cache_dir", lambda: shared_cache)
    specs_index.clear_lookup_cache()
    return shared_cache


# ── The bundled index (plan Task 3) ─────────────────────────────────────────


def test_lookup_finds_mist_wlans(built_cache):
    hits = specs_index.lookup("list wlans site", product="mist")
    assert any("/wlans" in h.get("path", "") for h in hits), hits[:3]
    assert all(h["product"] == "mist" for h in hits)


def test_exact_mist_endpoint(built_cache):
    hits = specs_index.lookup("GET /api/v1/sites/{site_id}/wlans")
    assert hits and hits[0]["method"] == "GET" and hits[0]["path"] == "/api/v1/sites/{site_id}/wlans"
    assert hits[0]["product"] == "mist"


def test_both_products_are_indexed(built_cache):
    conn = specs_index.connect()
    try:
        products = {r[0] for r in conn.execute("SELECT DISTINCT product FROM endpoints")}
    finally:
        conn.close()
    assert {"central", "mist"} <= products


def test_index_name_follows_the_bundle(built_cache):
    name = specs_index.index_path().name
    assert name.startswith("specs-") and name.endswith(".sqlite")
    assert specs_index.index_path().parent == built_cache


def test_deleting_the_cache_file_rebuilds(cache_dir):
    specs_index.connect().close()
    db_path = specs_index.index_path()
    assert db_path.exists()
    db_path.unlink()
    specs_index.connect().close()
    assert db_path.exists() and specs_index.marker_path().exists()


def test_missing_marker_rebuilds(cache_dir):
    specs_index.connect().close()
    specs_index.index_path().write_bytes(b"half-written")  # killed mid-build
    specs_index.marker_path().unlink()
    conn = specs_index.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] > 1000
    finally:
        conn.close()
    assert specs_index.marker_path().exists()


def _age(path, days):
    when = time.time() - days * 86400
    os.utime(path, (when, when))


def test_a_new_build_removes_old_bundle_indexes(cache_dir):
    cache_dir.mkdir(parents=True)
    stale = cache_dir / "specs-0000000000000000.sqlite"
    stale.write_bytes(b"old index")
    stale.with_name(stale.name + ".ok").write_text("ok\n")
    unmarked = cache_dir / "specs-1111111111111111.sqlite"
    unmarked.write_bytes(b"old index, no marker")
    for old in (stale, stale.with_name(stale.name + ".ok"), unmarked):
        _age(old, 40)
    keep = cache_dir / "notes.txt"
    keep.write_text("not ours")
    specs_index.connect().close()
    assert not stale.exists() and not stale.with_name(stale.name + ".ok").exists()
    assert not unmarked.exists()
    assert keep.exists() and specs_index.index_path().exists() and specs_index.marker_path().exists()


def test_an_index_another_version_used_lately_is_kept(cache_dir):
    # Two installed versions share the cache; neither may delete the other's index.
    cache_dir.mkdir(parents=True)
    other = cache_dir / "specs-0000000000000000.sqlite"
    other.write_bytes(b"other version's index")
    other.with_name(other.name + ".ok").write_text("ok\n")
    _age(other, 40)
    _age(other.with_name(other.name + ".ok"), 2)  # its marker was touched two days ago
    specs_index.connect().close()
    assert other.exists() and other.with_name(other.name + ".ok").exists()


def test_using_the_index_marks_it_as_recently_used(cache_dir):
    specs_index.connect().close()
    _age(specs_index.marker_path(), 40)
    specs_index.connect().close()
    assert time.time() - specs_index.marker_path().stat().st_mtime < 3600


def test_corrupt_cache_with_marker_rebuilds(cache_dir):
    specs_index.connect().close()
    specs_index.index_path().write_bytes(b"not a sqlite database at all, not even close")
    assert specs_index.lookup("GET /api/v1/self", product="mist")


# ── Where the cache folder is ──────────────────────────────────────────────


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_cache_folder_on_mac_and_linux(platform, monkeypatch, tmp_path):
    monkeypatch.setattr(specs_index.sys, "platform", platform)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # a Windows-only setting, not read here
    assert specs_index.cache_dir() == specs_index.Path.home() / ".cache" / "casper-network-mcp"


def test_cache_folder_on_windows_follows_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setattr(specs_index.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert specs_index.cache_dir() == tmp_path / "casper-network-mcp" / "Cache"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_cache_folder_on_windows_without_local_app_data(value, monkeypatch):
    # Not set, or blank: the usual AppData\Local place under the user's home.
    monkeypatch.setattr(specs_index.sys, "platform", "win32")
    if value is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", value)
    expected = specs_index.Path.home() / "AppData" / "Local" / "casper-network-mcp" / "Cache"
    assert specs_index.cache_dir() == expected


# ── Query groups ────────────────────────────────────────────────────────────


def test_drops_scaffolding_keeps_domain_terms():
    groups = specs_index._query_groups("What are the valid auth-type enum values for an auth profile?")
    flat = [s for g in groups for s in g]
    assert "auth-type" in flat and "profile" in flat
    for noise in ("what", "the", "valid", "enum", "values", "for"):
        assert noise not in flat


def test_hyphen_token_is_one_group_with_components():
    groups = specs_index._query_groups("device-firmware-upgrade endpoint")
    assert len(groups) == 1 and groups[0][0] == "device-firmware-upgrade"
    assert {"device", "firmware", "upgrade"} <= set(groups[0])


def test_irregular_plural_keeps_both_spellings():
    groups = specs_index._query_groups("authorization policies")
    group = next(g for g in groups if any(s.startswith("polic") for s in g))
    assert "policy" in group and "policie" in group


def test_stopwords_checked_before_stemming():
    assert [s for g in specs_index._query_groups("What does the opmode field accept?") for s in g] == ["opmode"]


def test_domain_synonyms_join_the_same_group():
    groups = specs_index._query_groups("ssid opmode")
    assert len(groups) == 2
    ssid = next(g for g in groups if "ssid" in g)
    assert "wlan" in ssid and "essid" in ssid


# ── Lookup over a fixture ───────────────────────────────────────────────────


def test_exact_method_path_returns_only_literal_operation(db):
    hits = specs_index.lookup("patch /device-firmware", db_path=db)
    assert len(hits) == 1 and hits[0]["kind"] == "endpoint"
    assert hits[0]["file_path"].endswith("firmware-management.json#PATCH /device-firmware")
    assert "Update device firmware settings" in hits[0]["text"]


def test_lookup_cache_returns_independent_copies(db):
    first = specs_index.lookup("patch /device-firmware", db_path=db)
    first[0]["text"] = "mutated"
    assert "mutated" not in specs_index.lookup("patch /device-firmware", db_path=db)[0]["text"]


def test_exact_operation_id_is_case_insensitive(db):
    hits = specs_index.lookup("UPDATEDEVICEFIRMWARE", db_path=db)
    assert len(hits) == 1 and hits[0]["path"] == "/device-firmware"


def test_exact_enum_hit_ranked_first_with_full_enum_list(db):
    hits = specs_index.lookup("What are the valid auth-type values for a CDA auth profile?", db_path=db)
    assert hits[0]["kind"] == "enum" and "cda-auth-profile.json" in hits[0]["file_path"]
    for value in ("MPSK", "EAP", "CAPTIVE_PORTAL", "MAB"):
        assert value in hits[0]["text"]


def test_endpoint_trim_resolves_hyphenated_token(db):
    hits = specs_index.lookup("Is there a device-firmware-upgrade endpoint?", db_path=db)
    assert any(h["kind"] == "endpoint" and "/device-firmware" in h["text"] for h in hits)


def test_off_corpus_query_returns_empty_not_noise(db):
    assert specs_index.lookup("How do I configure a BGP route reflector cluster identifier?", db_path=db) == []


def test_field_name_collision_needs_corroboration(db):
    hits = specs_index.lookup("What URL and method updates a CNAC MAC registration?", db_path=db)
    assert all("MvrpInterfaceConfig" not in h["file_path"] for h in hits)


def test_hyphen_components_do_not_self_corroborate(db):
    assert specs_index.lookup("auth-type quantum teleportation flux capacitor", db_path=db) == []


def test_top_k_caps_and_clamps(db):
    assert len(specs_index.lookup("cda auth profile firmware device", top_k=2, db_path=db)) <= 2
    assert len(specs_index.lookup("PATCH /device-firmware", top_k=0, db_path=db)) == 1


def test_hit_shape(db):
    hits = specs_index.lookup("auth-type for the CDA auth profile", db_path=db)
    assert hits
    for h in hits:
        assert {"text", "source", "file_path", "kind", "score", "product"} <= set(h)
        assert h["file_path"].startswith("specs/") and "#" in h["file_path"]


def test_get_enum_and_schema(db):
    assert specs_index.get_enum("auth-type", db_path=db)[0]["enums"] == ["MPSK", "EAP", "CAPTIVE_PORTAL", "MAB"]
    schema = specs_index.get_schema("CdaAuth", db_path=db)[0]
    assert schema["name"] == "CdaAuthProfile" and schema["fields"][0]["field_name"] == "auth-type"


def test_search_and_endpoint_helpers(db):
    assert specs_index.search("firmware", kind="endpoint", db_path=db)
    assert (
        specs_index.get_endpoint("device-firm", method="post", db_path=db)[0]["operation_id"] == "createDeviceFirmware"
    )


def test_response_description_resolves_refs_and_votes(db):
    assert specs_index.get_response_description("central", 429, db_path=db) == "Too many requests"
    assert specs_index.get_response_description("central", "404", db_path=db) == "Not here"
    assert specs_index.get_response_description("mist", 429, db_path=db) is None
    assert specs_index.get_response_description("", 429, db_path=db) is None


# ── Failure modes ───────────────────────────────────────────────────────────


def test_explicit_missing_db_raises_and_creates_nothing(tmp_path):
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(FileNotFoundError):
        specs_index.lookup("anything", db_path=missing)
    assert specs_index.get_response_description("central", 429, db_path=missing) is None
    assert not missing.exists()


def test_corrupt_explicit_db_raises_file_not_found_not_sqlite_error(tmp_path):
    bad = tmp_path / "specs.sqlite"
    bad.write_bytes(b"this is not a sqlite database, not even close!!")
    with pytest.raises(FileNotFoundError):
        specs_index.lookup("auth-type enum", db_path=bad)
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    with pytest.raises(FileNotFoundError):
        specs_index.lookup("firmware compliance", db_path=empty)
    assert specs_index.get_response_description("central", 429, db_path=bad) is None


def test_build_refuses_an_empty_corpus(tmp_path):
    (tmp_path / "specs").mkdir()
    with pytest.raises(RuntimeError):
        specs_index.build(tmp_path / "x.sqlite", specs_dir=tmp_path / "specs")
    assert not (tmp_path / "x.sqlite").exists()


def test_interrupted_build_preserves_prior_good_index(db, monkeypatch, tmp_path):
    good = db.read_bytes()

    def _boom(src, dst):
        raise RuntimeError("simulated crash during swap")

    monkeypatch.setattr(specs_index.os, "replace", _boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        specs_index.build(db, specs_dir=tmp_path / "specs")
    assert db.read_bytes() == good
    assert not list(tmp_path.glob("*.tmp*"))
