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
