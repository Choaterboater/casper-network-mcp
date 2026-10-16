"""Measure find_tool accuracy, ordering and warm latency without contacting a backend.

    uv run python scripts/bench_find_tool.py
    uv run python scripts/bench_find_tool.py --questions path/to/questions.yaml --json

The default remains the fixed 60-question development benchmark. Ranks and MRR
are bounded to ten results (an absent answer contributes zero). Latency retains
its original definition: product-filtered top-three calls, after warm-up.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import subprocess
import sys
import time
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = pathlib.Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "tests" / "bench" / "find_tool_questions.yaml"
ROUNDS = 3


def load_questions(path: pathlib.Path = QUESTIONS) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [{"q": str(d["q"]), "product": d.get("product"), "expect": list(d["expect"])} for d in data]


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def _rank(names: list[str], expected: set[str]) -> int | None:
    return next((i for i, name in enumerate(names, 1) if name in expected), None)


def _metrics(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    ranks = [row[mode]["expected_rank"] for row in rows]
    n = max(len(ranks), 1)
    return {
        "questions": len(ranks),
        "top1": round(sum(r == 1 for r in ranks) / n, 3),
        "top3": round(sum(r is not None and r <= 3 for r in ranks) / n, 3),
        "top10": round(sum(r is not None for r in ranks) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r is not None) / n, 4),
    }


def _failures(rows: list[dict[str, Any]], mode: str, cutoff: int) -> list[dict[str, Any]]:
    return [
        {"q": row["q"], "product": row["product"], "expect": row["expect"], **row[mode]}
        for row in rows
        if row[mode]["expected_rank"] is None or row[mode]["expected_rank"] > cutoff
    ]


def bench(questions: list[dict[str, Any]]) -> dict[str, Any]:
    from casper_network_mcp.router.find import find_tool

    find_tool("warm up", top_k=3)
    times: list[float] = []
    rows: list[dict[str, Any]] = []
    for item in questions:
        for _ in range(ROUNDS):
            start = time.perf_counter()
            find_tool(item["q"], top_k=3, product=item["product"])
            times.append((time.perf_counter() - start) * 1000)
        row = {"q": item["q"], "product": item["product"], "expect": item["expect"]}
        for mode, product in (("filtered", item["product"]), ("unfiltered", None)):
            names = [h["name"] for h in find_tool(item["q"], top_k=10, product=product)]
            row[mode] = {"got": names[:3], "top10": names, "expected_rank": _rank(names, set(item["expect"]))}
        rows.append(row)
    filtered = _metrics(rows, "filtered")
    unfiltered = _metrics(rows, "unfiltered")
    products = sorted({str(row["product"]) for row in rows if row["product"] is not None})
    return {
        **filtered,
        **{f"{key}_no_product": unfiltered[key] for key in ("top1", "top3", "top10", "mrr")},
        "by_product": {
            product: {
                mode: _metrics([row for row in rows if row["product"] == product], mode)
                for mode in ("filtered", "unfiltered")
            }
            for product in products
        },
        "p50_ms": round(statistics.median(times), 2) if times else 0.0,
        "p95_ms": round(_percentile(times, 95), 2) if times else 0.0,
        "misses": _failures(rows, "filtered", 3),
        "wrong_first": _failures(rows, "filtered", 1),
        "misses_no_product": _failures(rows, "unfiltered", 3),
        "wrong_first_no_product": _failures(rows, "unfiltered", 1),
        "results": rows,
    }


def cold_start_ms() -> float:
    """A fresh Python process: import the router and answer one question."""
    code = (
        "import time; t=time.perf_counter();"
        "from casper_network_mcp.router.find import find_tool;"
        "find_tool('bounce a switch port', top_k=3, product='central');"
        "print((time.perf_counter()-t)*1000)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    return round(float(out.stdout.strip().splitlines()[-1]), 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--questions", type=pathlib.Path, default=QUESTIONS, help="labelled YAML question file")
    args = parser.parse_args(argv)
    result = bench(load_questions(args.questions))
    result["cold_start_ms"] = cold_start_ms()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(
        f"questions {result['questions']}  top1 {result['top1']:.0%}  top3 {result['top3']:.0%}  "
        f"top3 without product {result['top3_no_product']:.0%}  "
        f"p50 {result['p50_ms']} ms  p95 {result['p95_ms']} ms  cold start {result['cold_start_ms']} ms"
    )
    for product, modes in result["by_product"].items():
        for mode, scores in modes.items():
            print(
                f"  {product} {mode}: n={scores['questions']}  top1 {scores['top1']:.1%}  "
                f"top3 {scores['top3']:.1%}  top10 {scores['top10']:.1%}  MRR {scores['mrr']:.4f}"
            )
    for mode, key in (("filtered", "wrong_first"), ("unfiltered", "wrong_first_no_product")):
        for miss in result[key]:
            rank = miss["expected_rank"]
            label = "top3 miss" if rank is None or rank > 3 else "wrong first"
            print(
                f"  {mode} {label} [{miss['product']}] {miss['q']!r}: "
                f"expected {miss['expect']}, rank {rank if rank is not None else '>10'}, got {miss['got']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
