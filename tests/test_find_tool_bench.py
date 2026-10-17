"""find_tool on the fixed 60-question set stays right and fast (Review Focus 4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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


BENCH_DIR = Path(__file__).parent / "bench"


def _baseline(filename):
    saved = json.loads((BENCH_DIR / "find_tool_baseline.json").read_text(encoding="utf-8"))["sets"][filename]
    # Windows checks text out with CRLF line endings: hash the text with LF so the same data has one hash.
    data = (BENCH_DIR / filename).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == saved["sha256"], "Frozen data changed"
    return saved


def test_fresh_evaluation_improves_mist_or_clearpass_without_any_product_regression():
    filename = "find_tool_evaluation.yaml"
    before = _baseline(filename)["by_product"]
    after = bench(load_questions(BENCH_DIR / filename))["by_product"]
    assert set(after) == set(before)
    for product in before:
        for mode in ("filtered", "unfiltered"):
            assert after[product][mode]["questions"] == before[product][mode]["questions"]
            for metric in ("top1", "top3"):
                assert after[product][mode][metric] >= before[product][mode][metric], (product, mode, metric, after)
    assert any(after[p]["filtered"]["top1"] > before[p]["filtered"]["top1"] for p in ("mist", "clearpass")), after


def test_original_benchmark_cannot_hide_one_products_regression_behind_another():
    before = _baseline("find_tool_questions.yaml")["by_product"]
    after = bench(load_questions())["by_product"]
    for product in before:
        for mode in ("filtered", "unfiltered"):
            for metric in ("top1", "top3"):
                assert after[product][mode][metric] >= before[product][mode][metric], (product, mode, metric, after)


def test_new_datasets_are_disjoint_and_have_only_existing_correct_product_labels():
    original = {q["q"] for q in load_questions()}
    development = load_questions(BENCH_DIR / "find_tool_development.yaml")
    evaluation = load_questions(BENCH_DIR / "find_tool_evaluation.yaml")
    dev_queries = {q["q"] for q in development}
    eval_queries = {q["q"] for q in evaluation}
    assert len(dev_queries) == len(development) == 30
    assert len(eval_queries) == len(evaluation) == 36
    assert not (dev_queries & eval_queries or original & eval_queries or original & dev_queries)
    entries = catalog().entries
    for question in development + evaluation:
        assert question["expect"]
        assert all(name in entries and entries[name].product == question["product"] for name in question["expect"])
    assert {p: sum(q["product"] == p for q in evaluation) for p in ("central", "mist", "clearpass")} == {
        "central": 12,
        "mist": 12,
        "clearpass": 12,
    }


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
