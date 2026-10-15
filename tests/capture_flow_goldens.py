"""One-shot golden capture for the ConnectorTranslateFlow parity harness.

HOW THE GOLDENS WERE PRODUCED
-----------------------------
Run this script ONCE against the PRE-refactor store.py (the four hand-written
sibling flows), from a pristine checkout of the base commit:

    python tests/capture_flow_goldens.py

It drives every scenario in ``tests/_flow_parity_fixtures.py`` and writes the
frozen (ok, msg, result-dict, SyncLog rows, config state) for each into
``tests/flow_engine_goldens.json``. That JSON is committed and is the byte-equal
target that ``tests/verify_flow_engine_parity.py`` re-asserts against the
POST-refactor engine. Do NOT regenerate it from the refactored code — the whole
point is that the goldens carry the OLD behavior forward.
"""

import json
import os

import _flow_parity_fixtures as fx

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_engine_goldens.json")


def main() -> None:
    records = fx.run_all()
    with open(_GOLDEN, "w", encoding="ascii") as f:
        json.dump(records, f, indent=2, sort_keys=True, ensure_ascii=True)
    print(f"captured {len(records)} scenario goldens -> {_GOLDEN}")


if __name__ == "__main__":
    main()
