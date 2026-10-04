from casper_network_mcp.core.redact import redact_sensitive, redact_tool_error_text


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
