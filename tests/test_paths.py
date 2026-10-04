import pytest

from casper_network_mcp.core.paths import UnsafePath, path_segment, safe_api_path, validate_product_base_url


@pytest.mark.parametrize("bad", ["a/b", "..", "x?y=1", "x#y", "", "a\nb", "%2e%2e"])
def test_path_segment_refuses(bad):
    with pytest.raises(UnsafePath):
        path_segment(bad)


@pytest.mark.parametrize("bad", [".", "a\\b", "a\x00b", "a\tb", "abc/../../orgs/X", "%2F", None, 7])
def test_path_segment_refuses_more(bad):
    with pytest.raises(UnsafePath):
        path_segment(bad)


def test_path_segment_keeps_plain_ids():
    assert path_segment("4b1c-77") == "4b1c-77"
    assert path_segment("00000000-0000-0000-0000-000000000001") == "00000000-0000-0000-0000-000000000001"


def test_path_segment_quotes_what_it_keeps():
    assert path_segment("Branch 12") == "Branch%2012"
    assert path_segment("a:b") == "a%3Ab"


def test_safe_api_path_stays_under_prefix():
    assert safe_api_path("/api/v1/sites/x/wlans", "/api/v1/") == "/api/v1/sites/x/wlans"
    with pytest.raises(UnsafePath):
        safe_api_path("/api/v1/../../admin", "/api/v1/")


@pytest.mark.parametrize(
    "bad",
    [
        "/admin/x",
        "https://evil.example.net/api/v1/x",
        "//evil.example.net/api/v1/x",
        "/api/v1/x?y=1",
        "/api/v1/x#y",
        "/api/v1/%2e%2e/admin",
        "/api/v1/%252e%252e/admin",
        "/api/v1/x%3Fy",
        "/api/v1/a\\b",
    ],
)
def test_safe_api_path_refuses(bad):
    with pytest.raises(UnsafePath):
        safe_api_path(bad, "/api/v1/")


def test_safe_api_path_accepts_several_prefixes():
    assert safe_api_path("/api/oauth/me", ("/api/v1/", "/api/")) == "/api/oauth/me"


def test_unsafe_path_is_a_value_error():
    assert issubclass(UnsafePath, ValueError)


def test_base_url_must_be_https_without_credentials():
    assert validate_product_base_url("https://api.mist.com/", product="Mist") == "https://api.mist.com"
    for bad in ("ftp://api.mist.com", "api.mist.com", "https://user:pw@api.mist.com", "https://"):
        with pytest.raises(ValueError):
            validate_product_base_url(bad, product="Mist")


def test_base_url_refuses_placeholder_hosts():
    for bad in ("https://clearpass.example.com", "https://changeme.corp.net", "https://x.invalid"):
        with pytest.raises(ValueError):
            validate_product_base_url(bad, product="ClearPass")


def test_base_url_allows_private_hosts_for_on_premises_products():
    # ClearPass usually lives on a private address; the person's own login is the limit.
    assert validate_product_base_url("https://198.51.100.20", product="ClearPass") == "https://198.51.100.20"
    assert validate_product_base_url("https://cppm.corp.lan", product="ClearPass") == "https://cppm.corp.lan"


def test_plain_http_only_for_this_machine():
    assert validate_product_base_url("http://127.0.0.1:8443", product="ClearPass") == "http://127.0.0.1:8443"
    with pytest.raises(ValueError):
        validate_product_base_url("http://198.51.100.20", product="ClearPass")
