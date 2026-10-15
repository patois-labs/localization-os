"""Capture tests/glossary_write_parity_golden.json from the CURRENT base -- the
LAW-TERM-1 write-parity golden for the terminology AUTHORITY INVERSION (T1b).

The golden is the id-normalized compat store + read-seam outputs the LEGACY write op stream
in ``verify_glossary_write_parity.capture_all()`` produces on the BASE tree with the concept
editor flag OFF and NO T1b product code (see that module's docstring and the T1b build
errata for the base SHA / flag / alembic head recorded in the golden header).

It is never regenerated from the post-change tree: after the inversion lands,
``verify_glossary_write_parity`` replays the SAME stream on the changed tree and asserts it
reproduces this golden, permitting only the enumerated DIFF-1/DIFF-2 shapes (expected empty).

The one permitted exception is a RE-PARENT: a reviewed change that INTENTIONALLY moves what the
legacy write stream emits, regenerated from the tree that already carries it and committed in the
same PR, with the ``base``/``note`` header below rewritten to name it. That has happened once, for
issue #497 (a re-import whose dnt cell is blank or missing now keeps the stored flag), which is why
the header names #497 rather than the T2 merge.

Run to (re)generate ONLY if the coordinator re-parents the T1b base SHA (A.5 / brief B "Base"):
    .venv\\Scripts\\python.exe tests/capture_glossary_write_parity.py
"""

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_glossary_write_parity as wp


def _base_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT).decode().strip()
    except Exception:
        return "unknown"


def main():
    snap = wp.capture_all()
    doc = {
        "_header": {
            "purpose": "LAW-TERM-1 write-parity golden (terminology T1b authority inversion)",
            "captured_from_sha": _base_sha(),
            "base": "main after issue #497 (glossary re-import keeps a blank/missing dnt)",
            "alembic_head": "b9e3d7c1f5a2",
            "flag_terminology_concept_editor": "OFF",
            "note": (
                "captured with NO T1b product code; re-parented off 400720ce because #497 "
                "intentionally changes what a re-import writes (a blank/missing dnt cell now "
                "keeps the stored flag); never regenerate from the post-change tree again"
            ),
        },
        "corpora": snap,
    }
    with open(wp._GOLDEN, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, sort_keys=True, ensure_ascii=False, indent=1)
    n_entries = sum(len(v["entries"]) for v in snap.values())
    print(f"wrote {wp._GOLDEN}: {len(snap)} corpora / {n_entries} entries")


if __name__ == "__main__":
    main()
