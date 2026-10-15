"""AU-7 — bounded list routes + server-side filters (gate rework regression locks).

Covers the four bounded /api list routes and the CLI paging flags:
  [A] GET /api/projects: limit/offset window + truncated flag, deterministic order
      (L8), server-side ?status= filter (M4), and loud 400s on bad params.
  [B] GET /api/customers: ?archived=only|all server-side filter (M4), the exact
      case-insensitive ?name= match that makes MCP create_project dedup pagination-
      proof (M5c), and the limit/offset window.
  [C] the CLI `project list` / `customer list` paging flags refuse a non-positive
      --limit / negative --offset LOUDLY (exit 2), not a silent clamp (L5).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"API LIST BOUNDS OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_listbounds_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import Customer, ProjectStatus
from app.services import apikeys, content, security

PW = "listbounds-pw-123456"
_N = [0]


def _token():
    _N[0] += 1
    u = security.create_user("lb%d@x.com" % _N[0], "LB", "admin", PW)  # noqa: UP031
    _row, raw = apikeys.create_key(u.id, "lb", scopes="api docs")
    return raw


def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _project(name, cust_id, status=None):
    with Session(engine) as s:
        p = content.project_create(
            s, name, source_locale="en-US", target_locales="de-DE", customer_id=str(cust_id)
        )
        if status is not None:
            p.status = status
            s.add(p)
            s.commit()
        return p.id


def test_projects_window_and_status_filter():
    client = TestClient(app)
    tok = _token()
    H = {"X-Loc-Token": tok}
    cid = _customer("LB Cust A")
    for i in range(5):
        _project("LBP%d" % i, cid, status=ProjectStatus.IN_TRANSLATION)  # noqa: UP031
    done_pid = _project("LBP-done", cid, status=ProjectStatus.COMPLETED)

    # full (no params): default hides done/closed -> 5, truncated False
    b = client.get("/api/projects", headers=H).json()
    assert b["truncated"] is False and b["limit"] == 1000 and b["offset"] == 0, b
    assert done_pid not in {p["id"] for p in b["projects"]}, ("default hides done", b)

    # window: limit=2 -> truncated True; offset pages deterministically (L8)
    p1 = client.get("/api/projects?limit=2", headers=H).json()
    assert len(p1["projects"]) == 2 and p1["truncated"] is True, p1
    p2 = client.get("/api/projects?limit=2&offset=2", headers=H).json()
    assert not ({p["id"] for p in p1["projects"]} & {p["id"] for p in p2["projects"]}), (
        "L8: paged windows must not overlap under a deterministic order",
        p1,
        p2,
    )

    # M4: ?status= is a SERVER-SIDE filter
    only_done = client.get("/api/projects?status=completed", headers=H).json()
    assert {p["id"] for p in only_done["projects"]} == {done_pid}, only_done
    all_st = client.get("/api/projects?status=all", headers=H).json()
    assert done_pid in {p["id"] for p in all_st["projects"]}, all_st

    # loud 400s: unknown status, non-int limit, zero limit, negative offset
    assert client.get("/api/projects?status=bogus", headers=H).status_code == 400
    assert client.get("/api/projects?limit=abc", headers=H).status_code == 400
    assert client.get("/api/projects?limit=0", headers=H).status_code == 400
    assert client.get("/api/projects?offset=-1", headers=H).status_code == 400
    print(
        "  [A] /api/projects: window + truncated + deterministic paging, server-side "
        "?status= filter, loud 400s on bad params OK"
    )


def test_customers_archived_and_name_match():
    client = TestClient(app)
    tok = _token()
    H = {"X-Loc-Token": tok}
    # NEW-4: a NON-ASCII name ('Ötzi' has a non-ASCII uppercase Ö) + a PADDED name --
    # both cases the old SQLite lower()==param SQL match got wrong. Revert-probe: this
    # test FAILS against the pre-fix `sqlfunc.lower(Customer.name) == name_q.lower()`
    # implementation (SQLite lower() is ASCII-only, and the stored column was untrimmed)
    # and passes against the casefold+trim Python match.
    active = _customer("Ötzi Ltd")
    padded = _customer("  Padded Co  ")
    with Session(engine) as s:
        arch = Customer(name="LB Archived Co")
        s.add(arch)
        s.commit()
        s.refresh(arch)
        from datetime import datetime

        arch.archived_at = datetime.utcnow()
        s.add(arch)
        s.commit()
        arch_id = arch.id

    # M4: archived modes are server-side
    active_only = client.get("/api/customers", headers=H).json()
    assert arch_id not in {c["id"] for c in active_only["customers"]}, active_only
    only_arch = client.get("/api/customers?archived=only", headers=H).json()
    assert {c["id"] for c in only_arch["customers"]} == {arch_id}, only_arch
    both = client.get("/api/customers?archived=all", headers=H).json()
    ids = {c["id"] for c in both["customers"]}
    assert active in ids and arch_id in ids, both
    assert client.get("/api/customers?archived=bogus", headers=H).status_code == 400

    # NEW-2/M5c: casefold+trim ?name= match (the dedup mechanism). A DIFFERENTLY-CASED
    # non-ASCII query must still hit the exact stored name -- 'ötzi ltd' matches
    # 'Ötzi Ltd' only under Unicode casefold, NOT SQLite's ASCII lower().
    m = client.get(
        "/api/customers", params={"name": "ötzi ltd", "archived": "all"}, headers=H
    ).json()
    assert {c["id"] for c in m["customers"]} == {active}, (
        "non-ASCII casefold match must resolve the exact stored customer",
        m,
    )
    # a PADDED query must hit the padded stored name (both sides trimmed)
    mp = client.get(
        "/api/customers", params={"name": "Padded Co", "archived": "all"}, headers=H
    ).json()
    assert {c["id"] for c in mp["customers"]} == {padded}, (
        "trim on both sides must match a padded stored name",
        mp,
    )
    none = client.get("/api/customers", params={"name": "no such co"}, headers=H).json()
    assert none["customers"] == [], none
    print(
        "  [B] /api/customers: server-side archived modes + casefold+trim ?name= "
        "match on non-ASCII + padded names (NEW-2 dedup mechanism) OK"
    )


def test_cli_paging_is_loud():
    """L5: a non-positive --limit / negative --offset is a usage error (exit 2), not a
    silent clamp -- matching the API's 400."""

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main(argv)
        return rc

    assert run(["project", "list", "--limit", "0"]) == 2, "--limit 0 must exit 2"
    assert run(["project", "list", "--offset", "-1"]) == 2, "--offset -1 must exit 2"
    assert run(["customer", "list", "--limit", "-3"]) == 2, "--limit -3 must exit 2"
    # a valid window still succeeds
    assert run(["project", "list", "--limit", "2"]) == 0
    print(
        "  [C] CLI project/customer list refuse a non-positive --limit / negative "
        "--offset with exit 2 (L5) OK"
    )


def main():
    init_db()
    from app.services import settings

    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    test_projects_window_and_status_filter()
    test_customers_archived_and_name_match()
    test_cli_paging_is_loud()
    print("API LIST BOUNDS OK")


from app.main import app

if __name__ == "__main__":
    main()
