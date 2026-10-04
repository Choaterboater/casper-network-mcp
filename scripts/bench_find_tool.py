"""Measure find_tool on the fixed question set: how often it is right, and how fast.

    uv run python scripts/bench_find_tool.py          # numbers, then every miss
    uv run python scripts/bench_find_tool.py --json   # the same as JSON

``bench(questions)`` returns top-1 and top-3 accuracy (each question passes its
product as find_tool's filter), the same top-3 without the product filter, and
p50/p95 time per call once the router is loaded. ``cold_start_ms()`` times a
fresh process from import to its first answer.
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

import yaml

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


def bench(questions: list[dict[str, Any]]) -> dict[str, Any]:
    from casper_network_mcp.router.find import find_tool

    find_tool("warm up", top_k=3)  # load the router once, as a running server has
    top1 = top3 = top3_any = 0
    times: list[float] = []
    misses: list[dict[str, Any]] = []
    for item in questions:
        expect = set(item["expect"])
        for _ in range(ROUNDS):
            start = time.perf_counter()
            hits = find_tool(item["q"], top_k=3, product=item["product"])
            times.append((time.perf_counter() - start) * 1000)
        names = [h["name"] for h in hits]
        if names[:1] and names[0] in expect:
            top1 += 1
        if expect & set(names):
            top3 += 1
        else:
            misses.append({"q": item["q"], "product": item["product"], "expect": item["expect"], "got": names})
        if expect & {h["name"] for h in find_tool(item["q"], top_k=3)}:
            top3_any += 1
    n = max(len(questions), 1)
    return {
        "questions": len(questions),
        "top1": round(top1 / n, 3),
        "top3": round(top3 / n, 3),
        "top3_no_product": round(top3_any / n, 3),
        "p50_ms": round(statistics.median(times), 2) if times else 0.0,
        "p95_ms": round(_percentile(times, 95), 2) if times else 0.0,
        "misses": misses,
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
    args = parser.parse_args(argv)
    result = bench(load_questions())
    result["cold_start_ms"] = cold_start_ms()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(
        f"questions {result['questions']}  top1 {result['top1']:.0%}  top3 {result['top3']:.0%}  "
        f"top3 without product {result['top3_no_product']:.0%}  "
        f"p50 {result['p50_ms']} ms  p95 {result['p95_ms']} ms  cold start {result['cold_start_ms']} ms"
    )
    for miss in result["misses"]:
        print(f"  miss [{miss['product']}] {miss['q']!r}: expected {miss['expect']}, got {miss['got']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
