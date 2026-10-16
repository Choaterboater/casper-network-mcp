"""The benchmark reports ordering failures, not just missing shortlists."""

from __future__ import annotations

import json

import pytest

from casper_network_mcp.router import find
from scripts import bench_find_tool as benchmark


def test_rank_two_is_wrong_first_but_not_top_three_miss(monkeypatch):
    monkeypatch.setattr(find, "find_tool", lambda *args, **kwargs: [{"name": "wrong"}, {"name": "right"}])
    result = benchmark.bench([{"q": "example", "product": "mist", "expect": ["right"]}])
    assert result["wrong_first"][0]["expected_rank"] == 2
    assert result["misses"] == []


def test_each_product_has_separate_filtered_and_unfiltered_scores(monkeypatch):
    def hits(query, *, product=None, **kwargs):
        return [{"name": f"{query}_right" if product else "wrong"}]

    monkeypatch.setattr(find, "find_tool", hits)
    products = ("central", "mist", "clearpass")
    questions = [{"q": p, "product": p, "expect": [f"{p}_right"]} for p in products]
    result = benchmark.bench(questions)
    assert set(result["by_product"]) == set(products)
    for modes in result["by_product"].values():
        assert modes["filtered"] == {"questions": 1, "top1": 1, "top3": 1, "top10": 1, "mrr": 1}
        assert modes["unfiltered"] == {"questions": 1, "top1": 0, "top3": 0, "top10": 0, "mrr": 0}


def test_reciprocal_rank_uses_first_acceptable_answer_and_zero_for_absent(monkeypatch):
    def hits(query, **kwargs):
        names = ["wrong", "other", "acceptable", "also_acceptable"] if query == "present" else ["wrong"]
        return [{"name": n} for n in names]

    monkeypatch.setattr(find, "find_tool", hits)
    result = benchmark.bench([
        {"q": q, "product": "mist", "expect": ["acceptable", "also_acceptable"]}
        for q in ("present", "absent")
    ])
    assert result["top1"] == 0
    assert result["top3"] == result["top10"] == 0.5
    assert result["mrr"] == pytest.approx(1 / 6, abs=0.0001)
    assert result["misses"][0]["expected_rank"] is None


def test_alternate_question_file_is_used_by_cli(tmp_path, monkeypatch, capsys):
    path = tmp_path / "questions.yaml"
    path.write_text('- q: custom\n  product: mist\n  expect: [right]\n', encoding="utf-8")
    monkeypatch.setattr(find, "find_tool", lambda *args, **kwargs: [{"name": "right"}])
    monkeypatch.setattr(benchmark, "cold_start_ms", lambda: 1.0)
    assert benchmark.main(["--questions", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["questions"] == 1
    assert result["results"][0]["q"] == "custom"


def test_text_report_labels_wrong_first_and_missing_shortlist_separately(tmp_path, monkeypatch, capsys):
    path = tmp_path / "questions.yaml"
    path.write_text(
        '- q: rank two\n  product: mist\n  expect: [right]\n'
        '- q: absent\n  product: mist\n  expect: [right]\n',
        encoding="utf-8",
    )

    def hits(query, **kwargs):
        names = ["wrong", "right"] if query == "rank two" else ["wrong"]
        return [{"name": name} for name in names]

    monkeypatch.setattr(find, "find_tool", hits)
    monkeypatch.setattr(benchmark, "cold_start_ms", lambda: 1.0)
    assert benchmark.main(["--questions", str(path)]) == 0
    text = capsys.readouterr().out
    assert "mist filtered: n=2" in text
    assert "mist unfiltered: n=2" in text
    assert "filtered wrong first [mist] 'rank two': expected ['right'], rank 2" in text
    assert "filtered top3 miss [mist] 'absent': expected ['right'], rank >10" in text
    assert "top3 miss [mist] 'rank two'" not in text


def test_empty_benchmark_has_zero_scores():
    result = benchmark.bench([])
    assert result["questions"] == 0
    assert result["top1"] == result["top3"] == result["top10"] == result["mrr"] == 0
    assert result["p50_ms"] == result["p95_ms"] == 0
    assert result["by_product"] == {}
