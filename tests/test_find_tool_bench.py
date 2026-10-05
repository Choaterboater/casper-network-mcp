"""find_tool on the fixed 60-question set stays right and fast (Review Focus 4)."""

from __future__ import annotations

from casper_network_mcp.router.index import catalog
from scripts.bench_find_tool import bench, load_questions


def test_question_set_shape():
    questions = load_questions()
    assert len(questions) == 60
    per_product = {p: sum(1 for q in questions if q["product"] == p) for p in ("central", "mist", "clearpass")}
    assert per_product == {"central": 20, "mist": 20, "clearpass": 20}


def test_every_expected_tool_exists():
    names = set(catalog().entries)
    unknown = sorted({n for q in load_questions() for n in q["expect"]} - names)
    assert unknown == []


def test_find_tool_stays_fast_and_right():
    r = bench(load_questions())
    assert r["top3"] >= 0.90, r["misses"]
    assert r["p95_ms"] <= 50


def test_review_focus_questions_are_in_the_top_three_and_fast():
    import time

    from casper_network_mcp.router.find import find_tool

    find_tool("warm up")
    for question, product, expected in (
        ("bounce port 7 on the closet switch", "central", "port_bounce"),
        ("who is on the guest wifi", "mist", "mist_list_site_clients"),
    ):
        start = time.perf_counter()
        names = [h["name"] for h in find_tool(question, top_k=3, product=product)]
        assert (time.perf_counter() - start) * 1000 <= 50
        assert expected in names, (question, names)


def test_plain_variants_outside_the_question_set_are_in_the_top_three():
    # Held out from the 60-question set: the same intents in other plain words.
    from casper_network_mcp.router.find import find_tool

    for question, product, expected in (
        ("reboot the access point", "mist", "mist_restart_site_device"),
        ("restart the access point", "mist", "mist_restart_site_device"),
        ("upgrade firmware on a switch", "central", "trigger_device_upgrade"),
        ("ping from a mist switch", "mist", "mist_ping_from_device"),
    ):
        names = [h["name"] for h in find_tool(question, top_k=3, product=product)]
        assert expected in names, (question, names)
