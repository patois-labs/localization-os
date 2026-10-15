"""Fetch the official TBX-Basic (DCA) schema files that strict TBX validation needs.

Localization OS does not ship these two files (the upstream LTAC-Global repositories publish
no licence). Strict TBX validation (`strict=true` on a TBX import) refuses with a clear message
until they are installed. This script downloads them once from the upstream raw URLs, checks the
size and sha256 recorded in `app/services/tbx_conformance.py`, and writes them next to each
other. Nothing is fetched at runtime by the product itself.

Usage:
    python scripts/fetch_tbx_schemas.py [--dest DIR]

Without --dest the files go to `app/resources/tbx/` (git-ignored for these names). Point the
product at another folder with the `LOC_TMS_TBX_SCHEMA_DIR` environment variable, or put them
in `<data dir>/tbx/`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import tbx_conformance


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", default=str(ROOT / "app" / "resources" / "tbx"))
    args = ap.parse_args(argv)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name, (url, size, sha) in tbx_conformance.SCHEMA_SOURCES.items():
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https URL
            data = resp.read()
        got = hashlib.sha256(data).hexdigest()
        if len(data) != size or got != sha:
            print(
                f"ERROR: {name} from {url} does not match the pinned size/sha256 "
                f"(got {len(data)} bytes, {got}). Not written.",
                file=sys.stderr,
            )
            return 1
        (dest / name).write_bytes(data)
        print(f"wrote {dest / name} ({len(data)} bytes, sha256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
