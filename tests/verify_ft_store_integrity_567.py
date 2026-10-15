"""Issue #567: the full-text TM writer (tm.ft_store) applies the approve-time integrity gate.

tm.ft_store upserts a whole-document body into FullTextTM on the translate path. Like the
segment writers, it stored a body that dropped or added a placeholder (or whose inline tokens
diverge), which a later identical document would re-serve as a 100% hit. Machine output has no
operator to acknowledge, so a violating body is simply not stored (returns None); a clean body,
a reordered body and an empty-placeholder body are unchanged.

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII only. Final line: "FT STORE INTEGRITY 567 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ft567_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import FullTextTM, Project
from app.services import tm

init_db()
_L, _R = "⟦", "⟧"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _count(pid):
    with Session(engine) as s:
        return len(s.exec(select(FullTextTM).where(FullTextTM.project_id == pid)).all())


def main():
    with Session(engine) as s:
        p = Project(name="ft567", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        src = "Dear {name}, you have %d items.\nThanks."
        bad = [
            "Estimado, tiene %d articulos.\nGracias.",
            "Estimado {name}, tiene %d articulos y {extra}.\nGracias.",
            "Estimado {name}, tiene %d articulos " + _L + "g1" + _R + ".\nGracias.",
        ]
        for i, body in enumerate(bad):
            if tm.ft_store(s, pid, src, body, "en-US", "es-ES") is not None:
                _fail(f"violating body {i} must not be stored")
        if _count(pid) != 0:
            _fail("nothing stored for violating bodies")
        ok = "Estimado {name}, tiene %d articulos.\nGracias."
        row = tm.ft_store(s, pid, src, ok, "en-US", "es-ES")
        if row is None or _count(pid) != 1:
            _fail("a clean body is still stored")
        # an update to a violating body must not overwrite the good one
        tm.ft_store(s, pid, src, bad[0], "en-US", "es-ES")
        if tm.ft_lookup(s, pid, src, "en-US", "es-ES") != ok:
            _fail("a violating update must not replace the stored body")
    print("FT STORE INTEGRITY 567 OK")


if __name__ == "__main__":
    main()
