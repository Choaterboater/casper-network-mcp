"""Build find_tool's word index, ``src/casper_network_mcp/router/index.json``.

Reads only the package itself (no network): every hand-written tool, every
generated tool and the spec lookup tools. The file ships in the wheel so the
first ``find_tool`` answers without loading any backend.

    python scripts/build_index.py           # write it
    python scripts/build_index.py --check   # exit 1 if the committed file is out of date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from casper_network_mcp.router.index import build_index, dumps_index

OUT = ROOT / "src" / "casper_network_mcp" / "router" / "index.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed index differs; write nothing")
    args = ap.parse_args(argv)
    text = dumps_index(build_index())
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("router/index.json is out of date: run scripts/build_index.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
