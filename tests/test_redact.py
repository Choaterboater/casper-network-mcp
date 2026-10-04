import json
import re
from pathlib import Path

import pytest

import casper_network_mcp
from casper_network_mcp.core.redact import _is_sensitive_key, _normalize_key, redact_sensitive, redact_tool_error_text

SPECS = Path(casper_network_mcp.__file__).parent / "specs"
REVIEWED = Path(__file__).with_name("redact_reviewed.txt")


def test_secret_keys_are_hidden():
    assert redact_sensitive({"passphrase": "s3cret", "psk": "x", "name": "Guest"}) == {
        "passphrase": "[hidden]",
        "psk": "[hidden]",
        "name": "Guest",
    }


def test_nested_lists_and_dicts():
    data = {"results": [{"name": "Guest", "passphrase": "x1"}, {"wlan": {"auth": {"psk": "y"}}}]}
    out = redact_sensitive(data)
    assert out["results"][0] == {"name": "Guest", "passphrase": "[hidden]"}
    # an "auth" object keeps its type, only the secrets inside are hidden
    assert out["results"][1]["wlan"]["auth"] == {"psk": "[hidden]"}


def test_auth_object_keeps_type_but_hides_keys():
    out = redact_sensitive({"auth": {"type": "psk", "psk": "x", "keys": ["k1", "k2"]}, "key": "v"})
    assert out == {"auth": {"type": "psk", "psk": "[hidden]", "keys": "[hidden]"}, "key": "[hidden]"}
    assert redact_sensitive({"auth": "Basic Zm9vOmJhcg=="}) == {"auth": "[hidden]"}


def test_key_spellings():
    out = redact_sensitive({"clientSecret": "a", "API-Key": "b", "accessToken": "c", "ssid": "Guest"})
    assert out == {"clientSecret": "[hidden]", "API-Key": "[hidden]", "accessToken": "[hidden]", "ssid": "Guest"}


def test_bearer_values_are_hidden():
    assert redact_sensitive(["Bearer abcdefgh12345678", "plain"]) == ["[hidden]", "plain"]


def test_secret_inside_a_stringified_blob():
    out = redact_sensitive({"meta": '{"password": "p", "site": "Branch-12"}'})
    assert 'p"' not in out["meta"] and "Branch-12" in out["meta"] and "[hidden]" in out["meta"]


def test_pagination_list_key_survives():
    out = redact_sensitive({"_pagination": {"list_key": "items", "token": "t"}})
    assert out["_pagination"] == {"list_key": "items", "token": "[hidden]"}


def test_error_text_masks_bearer_mid_string():
    text = "Error executing tool x: 401 with Bearer abcdefgh12345678 sent"
    assert "abcdefgh12345678" not in redact_tool_error_text(text)
    assert redact_tool_error_text("HTTP 401: Bearer token missing") == "HTTP 401: Bearer token missing"


def test_input_is_not_changed():
    data = {"psk": "x"}
    redact_sensitive(data)
    assert data == {"psk": "x"}


# ── credential fields found in the bundled specs ───────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "password_ntlm_hash",  # ClearPass GET /api/local-user
        "password_hash",
        "password-sha256",
        "community_name",  # Mist snmp_config.v2c_config[]
        "v3-community-name",
        "lldp-snmp-community",
        "keywrap_kek",  # Mist RADIUS server
        "keywrap_mack",
        "mfa_duo_skey",  # ClearPass guest config
        "mfa_duo_ikey",
        "password-plaintext",  # Central network services
        "password-ciphertext",
        "export_password2",
        "priv-pass-cypher",
        "auth-pass-text",
        "wpa-passphrase-value",
        "ldap-admin-password-value",
        "cak-ciphertext",
        "authentication-key-hexstring",
        "encryption-key-cipher-text",
        "key-value",
        "key_file",
        "url_hashkey",
        "auth_keys",
    ],
)
def test_spec_credential_fields_are_hidden(key):
    assert redact_sensitive({key: "s3cret", "name": "x"}) == {key: "[hidden]", "name": "x"}


@pytest.mark.parametrize(
    "key",
    ["password_minimum_length", "password-type", "passphrase_enabled", "psk_name", "key_type", "community", "hash"],
)
def test_policy_and_name_fields_stay_readable(key):
    assert redact_sensitive({key: 8}) == {key: 8}


#: Names that look like secrets but hold none (a setting, a name, an id, a
#: public key or a BGP community). Every property name in the bundled specs that
#: matches SECRET_LOOKING must be hidden by redact_sensitive or listed in
#: tests/redact_reviewed.txt, so a new secret field in a spec update fails here.
SECRET_LOOKING = re.compile(
    r"pass|secret|hash|communit|cred|psk|skey|ikey|cypher|cipher|kek|mack|private|token|key|cert"
)


def _spec_property_names() -> set[str]:
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(k for k in props if isinstance(k, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in SPECS.glob("*.json"):
        if path.name != "MANIFEST.json":
            walk(json.loads(path.read_text(encoding="utf-8")))
    return names


def test_every_secret_looking_spec_field_is_hidden_or_reviewed():
    reviewed = {
        line.strip()
        for line in REVIEWED.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    unreviewed = sorted(
        name
        for name in _spec_property_names()
        if SECRET_LOOKING.search(_normalize_key(name)) and not _is_sensitive_key(name) and name not in reviewed
    )
    assert not unreviewed, f"hide these in core/redact.py or add them to {REVIEWED.name}: {unreviewed}"
    stale = sorted(n for n in reviewed if _is_sensitive_key(n))
    assert not stale, f"{REVIEWED.name} lists names that are hidden anyway: {stale}"


def test_secret_settings_blocks_are_walked_or_hidden():
    out = redact_sensitive({"shared-secret-config": {"type": "CIPHER_TEXT", "ciphertext": "abc"}, "auth-key-info": "k"})
    assert out == {
        "shared-secret-config": {"type": "CIPHER_TEXT", "ciphertext": "[hidden]"},
        "auth-key-info": "[hidden]",
    }
