"""Write the hash-locked install file Casper uses: ``casper-network-mcp.lock.txt``.

    python scripts/make_lock.py 0.1.0                      # reads dist/, writes ./casper-network-mcp.lock.txt
    python scripts/make_lock.py 0.1.0 --dist dist --out casper-network-mcp.lock.txt

It is not a plain ``uv export`` (that writes this project as ``-e .``, which
Casper's ``--require-hashes --no-deps --only-binary :all:`` install refuses).
It is ``uv export --format requirements-txt --no-dev --hashes
--no-emit-project --frozen`` (every dependency pinned with its hashes, from
the committed ``uv.lock``), then one line for this package pinned to the
sha256 of the built wheel in ``dist/`` (the same bytes uploaded to PyPI).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "casper-network-mcp"
DIST_NAME = "casper_network_mcp"


def _uv() -> str:
    found = shutil.which("uv") or os.environ.get("UV")  # `uv run` sets UV to its own path
    if not found or not Path(found).exists():
        raise SystemExit("uv is needed: https://docs.astral.sh/uv/")
    return found


def wheel_for(version: str, dist: Path) -> Path:
    wheels = sorted(dist.glob(f"{DIST_NAME}-{version}-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one {DIST_NAME}-{version}-*.whl in {dist}, found {len(wheels)}")
    return wheels[0]


def lock_text(version: str, dist: Path, *, offline: bool = False) -> str:
    wheel = wheel_for(version, dist)
    cmd = [_uv(), "export", "--format", "requirements-txt", "--no-dev", "--hashes", "--no-emit-project", "--frozen"]
    if offline:
        cmd.append("--offline")
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"uv export failed:\n{out.stderr}")
    lines = [line for line in out.stdout.splitlines() if not line.startswith("#    uv export")]
    bad = [line for line in lines if line.startswith(("-e", "."))]
    if bad:
        raise SystemExit(f"uv export wrote a local project line: {bad[0]}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lines.append(f"{NAME}=={version} --hash=sha256:{digest}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", help="the version just built, e.g. 0.1.0")
    ap.add_argument("--dist", type=Path, default=ROOT / "dist", help="folder holding the built wheel (default dist/)")
    ap.add_argument("--out", type=Path, default=Path(f"{NAME}.lock.txt"), help="where to write the lock file")
    ap.add_argument("--offline", action="store_true", help="use only uv's local cache")
    args = ap.parse_args(argv)
    text = lock_text(args.version, args.dist, offline=args.offline)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({text.count('==')} pinned packages)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
