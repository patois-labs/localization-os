"""Capture tests/glossary_bulk_golden.json ONCE from the PER-ENTRY (pre-change) glossary
concept reconcile -- the flow-engine golden pattern (see tests/capture_flow_goldens.py).

The committed golden is the id-free store the per-entry ``sync_entry_concept`` /
``_sync_glossary_from_entries`` path produces for the rich fixture in
``verify_glossary_bulk_import_scaling`` (RICH_CSV then RICH_CSV_REIMPORT). That per-entry
path is UNCHANGED by the terminology-bulk train, so this golden equals the one the
pre-change (main 1e91de9f) reconcile would have written. ``verify_glossary_bulk_import_
scaling.test_equivalence_golden`` then asserts the SHIPPED bulk path reproduces it byte-for-
byte.

Run to (re)generate after an INTENTIONAL store change:
    .venv\\Scripts\\python.exe tests/capture_glossary_bulk_golden.py
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_glossary_bulk_import_scaling as vb


def main():
    gid = vb._new_glossary()
    vb._import(gid, vb.RICH_CSV, use_bulk=False)
    vb._import(gid, vb.RICH_CSV_REIMPORT, use_bulk=False)
    snap = vb._snapshot(gid)
    with open(vb._GOLDEN, "w", encoding="ascii") as f:
        json.dump(snap, f, sort_keys=True, ensure_ascii=True, indent=1)
    print(
        f"wrote {vb._GOLDEN}: {snap['n_concepts']} concepts / {snap['n_terms']} terms / "
        f"{snap['n_entries']} entries / {snap['n_senses']} senses"
    )


if __name__ == "__main__":
    main()
