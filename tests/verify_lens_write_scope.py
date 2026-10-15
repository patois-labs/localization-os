"""GitHub #464 follow-up: a quality lens is READ from every ring it is shared with, but only
WRITTEN (edited in place, archived) from the ring that owns it.

`get_lens_by_id` lets a lens with a NULL org or NULL customer through to every narrower ring,
which is the right read rule. `update_lens` and `archive_lens` used it as their only gate, so a
customer-scoped caller could edit or archive an org-level custom lens, and an org-scoped caller a
global one, changing it for everyone it is shared with.

  read      a shared lens is still visible (get_lens_by_id) from the narrower ring.
  update    in place from the owning ring works; from a narrower ring it is refused (None).
  builtin   editing a builtin still forks into the caller's own ring (unchanged).
  archive   from the owning ring works; from a narrower or different ring it is refused (None).

Final line: "LENS WRITE SCOPE OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_lens_write_scope_")

from sqlmodel import Session

from app.db import engine, init_db
from app.services import quality_lens as ql

init_db()
FAILS = []


def check(cond, label):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def _cfg(title):
    return {
        "title": title,
        "severities": list(ql.SEED_SEVERITIES),
        "weights": dict(ql.SEED_WEIGHTS),
        "block_severity": ql.SEED_BLOCK_SEVERITY,
    }


with Session(engine) as s:
    org_lens = ql.create_lens(s, org_id=1, customer_id=None, created_by="t", **_cfg("Org wide"))
    glob_lens = ql.create_lens(
        s, org_id=None, customer_id=None, created_by="t", **_cfg("Global wide")
    )
    cust_lens = ql.create_lens(s, org_id=1, customer_id=7, created_by="t", **_cfg("Cust only"))
    oid, gid, cid = org_lens.id, glob_lens.id, cust_lens.id

    # read: still shared down
    check(
        ql.get_lens_by_id(s, oid, org_id=1, customer_id=7) is not None,
        "org lens readable from its customer",
    )
    check(
        ql.get_lens_by_id(s, gid, org_id=1, customer_id=None) is not None,
        "global lens readable from an org",
    )

    # update from a narrower ring: refused, and nothing changed
    check(
        ql.update_lens(s, oid, org_id=1, customer_id=7, title="Hijacked") is None,
        "customer ring cannot edit the org lens",
    )
    check(
        ql.update_lens(s, gid, org_id=1, customer_id=None, title="Hijacked") is None,
        "org ring cannot edit the global lens",
    )
    s.expire_all()
    check(ql.get_lens_by_id(s, oid, org_id=1).title == "Org wide", "org lens title unchanged")
    check(
        ql.get_lens_by_id(s, gid, org_id=None).title == "Global wide", "global lens title unchanged"
    )

    # update from the owning ring: works
    got = ql.update_lens(s, oid, org_id=1, customer_id=None, title="Org renamed")
    check(got is not None and got.title == "Org renamed", "owning org ring edits its lens in place")
    got = ql.update_lens(s, cid, org_id=1, customer_id=7, title="Cust renamed")
    check(
        got is not None and got.title == "Cust renamed",
        "owning customer ring edits its lens in place",
    )
    got = ql.update_lens(s, gid, org_id=None, customer_id=None, title="Global renamed")
    check(
        got is not None and got.title == "Global renamed",
        "the install-wide ring edits its own lens",
    )

    # a builtin still forks into the caller's ring
    base = ql.ensure_builtins(s)["esa"]
    fork = ql.update_lens(s, base.id, org_id=1, customer_id=7, threshold=80)
    check(
        fork is not None and fork.id != base.id and fork.customer_id == 7,
        "a builtin edit forks into the caller ring",
    )

    # archive
    check(
        ql.archive_lens(s, oid, org_id=1, customer_id=7) is None,
        "customer ring cannot archive the org lens",
    )
    check(
        ql.archive_lens(s, gid, org_id=1, customer_id=None) is None,
        "org ring cannot archive the global lens",
    )
    check(
        ql.archive_lens(s, cid, org_id=2, customer_id=7) is None,
        "a foreign org cannot archive a customer lens",
    )
    s.expire_all()
    check(ql.get_lens_by_id(s, oid, org_id=1).archived_at is None, "org lens still live")
    check(ql.get_lens_by_id(s, gid, org_id=None).archived_at is None, "global lens still live")
    check(
        ql.archive_lens(s, oid, org_id=1, customer_id=None) is not None,
        "owning org ring archives its lens",
    )
    check(
        ql.archive_lens(s, cid, org_id=1, customer_id=7) is not None,
        "owning customer ring archives its lens",
    )

if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("LENS WRITE SCOPE OK")
