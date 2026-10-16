"""General routing rules, using vocabulary and synthetic list/get pairs."""

from __future__ import annotations

import pytest

from casper_network_mcp.router import find, index


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("SSIDS", {"wlan"}),
        ("health checks", {"onguard"}),
        ("access points", {"ap"}),
        ("login failures", {"auth", "failure"}),
        ("wi-fi", {"wlan", "ssid"}),
        ("802.1x", {"dot1x", "service"}),
    ],
)
def test_synonyms_match_folded_words_and_phrases(query, expected):
    assert expected <= find.query_tokens(query)


@pytest.mark.parametrize("query", ["xssids", "ssidsuffix", "unhealth checks", "health checklists"])
def test_synonyms_still_require_whole_words(query):
    assert not {"wlan", "onguard"} & find.query_tokens(query)


def test_plural_synonym_is_still_one_idea():
    assert find.query_groups("SSIDs") == [frozenset({"ssid", "wlan"})]


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("failed", {"failure"}),
        ("denied", {"failure"}),
        ("add", {"create"}),
        ("history", {"event"}),
        ("connection", {"event"}),
        ("interface", {"port"}),
        ("drop", {"flapping"}),
        ("dropping", {"flapping"}),
        ("reconnect", {"flapping"}),
        ("reconnecting", {"flapping"}),
        ("flap", {"flapping"}),
        ("shut", {"bounce"}),
        ("re-enable", {"bounce"}),
        ("extend", {"update"}),
        ("block", {"update"}),
        ("mark", {"update"}),
    ],
)
def test_missing_vocabulary(word, expected):
    assert expected <= find.query_tokens(word)


@pytest.mark.parametrize("word", ["flap", "drop", "dropping", "reconnect", "reconnecting"])
def test_flapping_is_not_a_request_to_bounce(word):
    assert "bounce" not in find.query_tokens(word)


@pytest.mark.parametrize(
    ("word", "expected"),
    [("connected", {"session"}), ("online", {"session"}), ("switch", {"network", "device"})],
)
def test_clearpass_vocabulary_is_product_specific(word, expected):
    assert expected <= set().union(*find.query_groups(word, product="clearpass"))
    assert expected <= find.query_tokens(f"clearpass {word}")
    assert expected <= find.query_tokens(f"cppm {word}")
    for product in (None, "mist", "central"):
        assert not expected & set().union(*find.query_groups(word, product=product))


@pytest.mark.parametrize(
    ("word", "expected"),
    [("connected", {"session"}), ("online", {"session"}), ("switch", {"network", "device"})],
)
def test_find_tool_applies_product_vocabulary(word, expected):
    hits = find.find_tool(word, top_k=3, product="ClearPass")
    assert hits
    assert all(hit["product"] == "clearpass" for hit in hits)
    assert any(expected <= index.tokens(hit["name"]) for hit in hits)


@pytest.mark.parametrize("word", ["bounce", "shut", "re-enable"])
def test_disruptive_action_intent(word):
    assert find.intent(f"please {word} a port") == "act"


@pytest.mark.parametrize("query", ["please - bounce a port", "- bounce a port", "reboot-access-point"])
def test_action_intent_keeps_existing_punctuation_boundaries(query):
    assert find.intent(query) == "act"


@pytest.mark.parametrize("word", ["extend", "block", "mark"])
def test_update_intent(word):
    assert find.intent(f"can you {word} an account") == "update"


@pytest.mark.parametrize("product", ["mist", "central", "aruba", "clearpass", "cppm"])
@pytest.mark.parametrize("action_query", ["create a policy", "remove a policy", "show policies", "restart a device"])
def test_leading_product_does_not_hide_intent(product, action_query):
    assert find.intent(f"please {product}: {action_query}") == find.intent(action_query)


@pytest.mark.parametrize("query", ["policy delete example", "unknown create a policy"])
def test_intent_still_stops_at_an_unknown_leading_word(query):
    assert find.intent(query) is None


@pytest.mark.parametrize(
    ("query", "product", "expected"),
    [
        ("central create a webhook", "central", {"create_webhook"}),
        ("aruba remove a report", "central", {"delete_report"}),
        ("mist create a wlan", "mist", {"mist_create_site_wlan", "mist_create_org_wlan"}),
        ("mist remove a wlan", "mist", {"mist_delete_wlan", "mist_delete_site_wlan"}),
        ("mist restart an access point", "mist", {"mist_restart_site_device"}),
        ("clearpass remove a guest", "clearpass", {"clearpass_delete_guest"}),
    ],
)
def test_product_prefixed_requests_rank_the_requested_action_first(query, product, expected):
    assert find.find_tool(query, top_k=1, product=product)[0]["name"] in expected


@pytest.fixture
def paired_tools(monkeypatch):
    entries = tuple(
        index.IndexEntry(
            name=f"mist_{verb}_{noun}",
            product="mist",
            summary="Read the resource.",
            kind="read",
            label="read",
            origin="generated",
            method="GET",
            path=f"/api/{noun}" + ("/{id}" if verb == "get" else ""),
            operation_id=f"mist_{verb}_{noun}",
            name_tokens=frozenset(index.tokens(f"mist_{verb}_{noun}")),
            summary_tokens=frozenset(index.tokens("Read the resource.")),
            params=frozenset(),
        )
        for verb, noun in (("get", "site"), ("list", "sites"), ("get", "policy"), ("list", "policies"))
    )
    postings = {
        word: tuple(i for i, entry in enumerate(entries) if word in entry.name_tokens)
        for entry in entries
        for word in entry.name_tokens
    }
    word_index = index.WordIndex(entries, {e.name: e for e in entries}, postings)
    monkeypatch.setattr(find, "load_index", lambda: word_index)
    return word_index


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("show sites", "mist_list_sites"),
        ("get policies", "mist_list_policies"),
        ("list site", "mist_list_sites"),
        ("show all site", "mist_list_sites"),
        ("show site Branch-1", "mist_get_site"),
        ("get policy named Example", "mist_get_policy"),
    ],
)
def test_collection_and_single_resource_preference(paired_tools, query, expected):
    assert find.find_tool(query, top_k=1, product="mist")[0]["name"] == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [("mist_get_site", "mist_get_site"), ("GET /api/sites", "mist_list_sites")],
)
def test_exact_lookup_overrides_read_form_preference(paired_tools, query, expected):
    assert find.find_tool(query, top_k=1)[0]["name"] == expected


def test_exact_name_and_api_path_keep_precedence(paired_tools, monkeypatch):
    # Make the competing getter win ordinary scoring, but not an exact lookup.
    monkeypatch.setattr(find, "_score", lambda entry, *args: 1000 if entry.name == "mist_get_site" else 0)
    for query in ("mist_list_sites", "GET /api/sites"):
        assert find.find_tool(query, top_k=3)[0]["name"] == "mist_list_sites"


def test_flapping_is_not_bounce_and_collection_stays_distinct_from_one_resource(paired_tools, monkeypatch):
    # Check real diagnostic routing before using the synthetic list/get pair.
    with monkeypatch.context() as patch:
        patch.setattr(find, "load_index", index.load_index)
        hits = find.find_tool("port keeps flapping", top_k=3, product="central")
        assert hits
        assert hits[0]["kind"] in {"read", "troubleshoot"}
        assert hits[0]["name"] != "port_bounce"
    assert find.find_tool("mist list sites", top_k=1)[0]["name"] == "mist_list_sites"
    assert find.find_tool("mist get site Branch-1", top_k=1)[0]["name"] == "mist_get_site"
