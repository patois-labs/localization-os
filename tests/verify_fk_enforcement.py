"""Guard the guard: assert the app engine ACTUALLY enforces foreign keys.

SQLite ignores FK constraints unless every connection issues
``PRAGMA foreign_keys=ON`` (a decades-old back-compat default). app/db.py wires
that pragma into the engine's per-connection ``connect`` hook so the zero-config /
test engine enforces the same FKs Postgres always has. This closed a real blind
spot: an FK-invalid write (the glossary ``project_id=0`` sentinel) once passed a
fully-green SQLite suite because the constraint was silently ignored.

This hermetic test pins that behaviour so a future engine refactor can't quietly
drop the pragma and re-open the hole:

  1. the pragma reports ON on a fresh connection;
  2. a dangling-FK INSERT is REJECTED with an IntegrityError (the constraint is
     actually enforced, not merely declared);
  3. a well-formed FK write still succeeds (enforcement isn't over-broad).

Offline + throwaway data dir; no provider calls. Final line: "FK ENFORCEMENT OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_fkenforce_")

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def test_pragma_reports_on():
    """A fresh connection from the app engine must report foreign_keys=ON.

    Postgres has no such pragma and always enforces FKs, so this SQLite-only check
    is skipped (cleanly) on any other backend — the enforcement is inherent there.
    """
    if engine.dialect.name != "sqlite":
        print("  pragma: SKIPPED (non-sqlite backend enforces FKs inherently)")
        return
    with engine.connect() as conn:
        val = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert val == 1, f"PRAGMA foreign_keys must be 1 (ON), got {val!r}"
    print("  pragma: foreign_keys=ON on a fresh app-engine connection OK")


def test_dangling_fk_insert_rejected():
    """A write that violates a real FK must raise IntegrityError.

    document.project_id -> project.id: inserting a Document pointing at a project
    id that does not exist is the exact shape of the bug class this guards. With
    enforcement ON it must be REJECTED (on Postgres it always was)."""
    raised = False
    with Session(engine) as s:
        s.add(Document(project_id=987654321, filename="dangling.txt", fmt="txt"))
        try:
            s.commit()
        except IntegrityError:
            raised = True
            s.rollback()
    if not raised:
        _fail("a dangling-FK Document INSERT was NOT rejected — FK enforcement is OFF")
    print("  reject: dangling document.project_id INSERT -> IntegrityError OK")


def test_valid_fk_insert_succeeds():
    """Enforcement must not be over-broad: a well-formed FK write still commits.

    Create a real Project, then a Document pointing at it — this must succeed."""
    from app.models import Project

    with Session(engine) as s:
        p = Project(name="FK guard project", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="ok.txt", fmt="txt")
        s.add(d)
        s.commit()
        s.refresh(d)
        assert d.id is not None and d.project_id == p.id
    print("  allow: a well-formed FK Document INSERT commits OK")


def main():
    init_db()
    test_pragma_reports_on()
    test_dangling_fk_insert_rejected()
    test_valid_fk_insert_succeeds()
    print("FK ENFORCEMENT OK")


if __name__ == "__main__":
    main()
