"""Q3 gate regression guard: the auto-inbox WRITE seam must never persist the #73
poison sentinel (access.BLOCKED_ORG_ID).

routing.ensure_department_inbox STAMPS org_id onto a newly-created inbox Project. Its
callers pass org_id=_ident_org_id(ident), which (post-#73) resolves an org-less principal
to access.BLOCKED_ORG_ID (-1) rather than None. Every OTHER principal-derived write stamp
in the train (content._org_for, security.create_user, api_cli user-create,
llmemory.write_org_id) collapses that sentinel back to None via access.stampable_org_id;
this was the one write seam that did not. This test PINS the collapse: handed the poison
(the exact value the resolved scope now carries), the inbox seam stamps NULL, never -1.

Reachability of the un-collapsed seam is walled today (POST /api/documents is behind
deps._require_doc_scope, which admits only user-kind identities, all of them
user_org_active-gated so a NULL-org principal is 401'd) and a -1 stamp is additionally
FK-rejected (Project.org_id -> organization.id). This guard is the defense-in-depth pin:
a future doc-scope grant or a new write route re-using _ident_org_id must not re-open it.

ASCII-only; standalone one-process-per-suite script.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_q3_inbox_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Project
from app.services import access, routing


def test_inbox_stamp_collapses_the_poison() -> None:
    """A would-be org-less caller (resolved scope == BLOCKED_ORG_ID) provisions an inbox
    that is stamped NULL, not -1 -- the sentinel never reaches a persisted column."""
    init_db()
    with Session(engine) as s:
        proj = routing.ensure_department_inbox(
            s, "q3-guard-dept", "q3-guard-actor", org_id=access.BLOCKED_ORG_ID
        )
        pid = proj.id
    with Session(engine) as s:
        row = s.get(Project, pid)
        assert row.org_id is None, (
            "Q3 #73: the auto-inbox write seam stamped the poison sentinel "
            f"(expected NULL, got {row.org_id!r})"
        )
    print("  Q3 inbox seam: BLOCKED_ORG_ID collapses to NULL on the inbox stamp")


def test_inbox_stamp_passes_a_real_org_through() -> None:
    """The collapse is byte-identical for every legitimate caller: a real org id is
    stamped unchanged; UNSCOPED (the authorized operator door) stamps NULL."""
    init_db()
    from app.models import Organization

    with Session(engine) as s:
        org = Organization(name="Q3 Guard Org", active=True)
        s.add(org)
        s.commit()
        s.refresh(org)
        oid = org.id
    with Session(engine) as s:
        proj = routing.ensure_department_inbox(s, "q3-real-dept", "q3-real-actor", org_id=oid)
        assert proj.org_id == oid, ("a real org id must pass through unchanged", proj.org_id)
    with Session(engine) as s:
        proj = routing.ensure_department_inbox(
            s, "q3-unscoped-dept", "q3-unscoped-actor", org_id=access.UNSCOPED
        )
        assert proj.org_id is None, ("UNSCOPED must stamp NULL", proj.org_id)
    print("  Q3 inbox seam: a real org passes through; UNSCOPED stamps NULL")


def main() -> None:
    test_inbox_stamp_collapses_the_poison()
    test_inbox_stamp_passes_a_real_org_through()
    print("Q3 INBOX STAMP OK")


if __name__ == "__main__":
    main()
