"""Build the generated-tool manifests from the bundled vendor specs.

Reads only ``src/casper_network_mcp/specs/`` (no network) and writes
``src/casper_network_mcp/openapi_gen/manifests/<product>.json``.

    python scripts/build_manifests.py                    # every product
    python scripts/build_manifests.py --product mist     # one product
    python scripts/build_manifests.py --check            # compare, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from casper_network_mcp.openapi_gen.manifest import build_product_manifest, dumps
from casper_network_mcp.specs_bundle import PRODUCTS

OUT_DIR = ROOT / "src" / "casper_network_mcp" / "openapi_gen" / "manifests"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--product", choices=PRODUCTS, action="append", help="build only this product (repeatable)")
    ap.add_argument("--check", action="store_true", help="exit 1 if a committed manifest differs; write nothing")
    args = ap.parse_args(argv)

    stale = []
    for product in args.product or PRODUCTS:
        manifest = build_product_manifest(product)
        text = dumps(manifest)
        path = OUT_DIR / f"{product}.json"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(product)
            continue
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        print(f"{product}: {manifest['source']['operation_count']} tools -> {path.relative_to(ROOT)}", file=sys.stderr)
    if stale:
        print(f"out of date: {', '.join(stale)} (run scripts/build_manifests.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
